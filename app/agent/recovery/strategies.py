"""The recovery ladder (spec section 4.8).

Ordered cheapest and most likely first. Each rung answers a different reason a
click can miss, and the order is roughly how often each reason turns up:

1. The page re-rendered and the element moved.
2. The ids changed but the control is still the same button.
3. Same, but it has no accessible role worth speaking of.
4. Everything moved but the original path still resolves.
5. It is below the fold and was never clickable.
6. Something is sitting on top of it.
7. It is there but nothing can name it, so look at the pixels.

Every strategy does the same three things: work out a better locator, run the
original action against it, and let the verifier decide whether that worked.
None of them may declare success on their own, because a strategy that grades
its own attempt is a strategy that always passes.
"""

import difflib
from typing import Final, Protocol

from app.agent.browser.action_builder import build_action, selector_for
from app.agent.contracts import AttemptResult, Grounder, RecoveryContext
from app.agent.perception.dom_extractor import ElementInfo
from app.agent.perception.overlays import icon_closers
from app.agent.perception.state import PageState
from app.agent.planner.schema import Decision
from app.core.logging import get_logger
from app.domain.enums import ActionType, RecoveryOutcome

__all__ = [
    "ORDERED_STRATEGIES",
    "DismissOverlay",
    "RecoveryStrategy",
    "ReperceiveByIndex",
    "RoleNameLocator",
    "ScrollAndWait",
    "StoredSelector",
    "TextLocator",
    "VisualGrounding",
    "strategies_with_grounding",
]

logger = get_logger(__name__)

# difflib ratio at or above this counts as the same label. Below it, a button
# called "Search" starts matching one called "Clear".
FUZZY_THRESHOLD: Final = 0.65

# What a close button usually says. Ordered so the least destructive reading
# of the overlay wins: reject before accept, because clicking "Accept all"
# consents to something on the user's behalf.
OVERLAY_LABELS: Final[tuple[str, ...]] = (
    "reject non-essential",
    "reject all",
    "reject",
    "decline",
    "no thanks",
    "not now",
    "maybe later",
    "close",
    "dismiss",
    "×",  # noqa: RUF001  (real close buttons use the multiplication sign)
    "✕",
    "x",
    "accept all cookies",
    "accept all",
    "accept",
    "got it",
    "ok",
)

# Roles the `role=` locator engine understands, for elements the collector did
# not give an explicit role.
# Cookie banner plus newsletter popup is two; a third is a page that is
# fighting back, and the retry budget is a better place to give up.
MAX_OVERLAYS: Final = 3

TAG_ROLES: Final[dict[str, str]] = {
    "button": "button",
    "a": "link",
    "input": "textbox",
    "textarea": "textbox",
    "select": "combobox",
}


class RecoveryStrategy(Protocol):
    """One rung. Ordered by :data:`ORDERED_STRATEGIES`."""

    name: str
    label: str

    def applicable(self, ctx: RecoveryContext) -> bool:
        """Is there any point trying this one?"""
        ...

    async def attempt(self, ctx: RecoveryContext) -> AttemptResult: ...


async def retry_with(ctx: RecoveryContext, selector: str) -> AttemptResult:
    """Run the original action against a new locator, then re-verify.

    The verifier is the judge, not the strategy. A strategy that decides for
    itself whether it worked is one that reports success whenever Playwright
    did not raise, which is most of the time and almost never the question.
    """
    # The baseline is the page as it is now, not as it was when the step
    # first failed. A strategy that changes the page itself, such as one that
    # closes a modal, would otherwise satisfy `dom_changed` on its own
    # cleanup and report a click that never landed as a success.
    baseline = await ctx.browser.perceive()

    try:
        action = build_action(ctx.decision, ctx.element, selector=selector)
        result = await ctx.browser.execute(action)
    except Exception as exc:
        return AttemptResult(RecoveryOutcome.FAILED, error=type(exc).__name__)

    if not result.ok:
        return AttemptResult(
            RecoveryOutcome.FAILED,
            error=result.error_code.value if result.error_code else "action_failed",
        )

    await ctx.browser.settle()
    after = await ctx.browser.perceive()
    ctx.after = after

    verdict = await ctx.verifier.check(ctx.decision, baseline, after, result)
    outcome = RecoveryOutcome.SUCCEEDED if verdict.ok else RecoveryOutcome.FAILED
    return AttemptResult(outcome, verdict=verdict, error=None if verdict.ok else verdict.detail)


def blocked_by_overlay(ctx: RecoveryContext) -> bool:
    """Is something sitting on top of the target?

    Re-finding an element that is covered achieves nothing: the locator was
    never the problem, the modal on top of it was. Saying so in
    ``applicable`` rather than letting those rungs run and fail is what keeps
    the retry budget for the one rung that can help. Without it a cookie
    banner spends all three attempts on rungs one to three and the ladder
    never reaches the one that closes it.
    """
    if ctx.element is None:
        return False
    if ctx.element.covered:
        return True

    # The overlay may have arrived after the state that failed was captured,
    # which is the popup chaos case.
    wanted = _signature(ctx.element)
    current = next((item for item in ctx.after.elements if _signature(item) == wanted), None)
    if current is not None:
        return current.covered

    # Gone from the page entirely while something closeable is on screen: the
    # collector withholds a fully covered element, so this is what a modal
    # over the target looks like.
    return bool(DismissOverlay.closers(ctx.after))


def relocate(ctx: RecoveryContext, state: PageState) -> str | None:
    """The target's selector in a freshly read page.

    Perception assigns indices on every read, so a strategy that re-perceives
    and then reuses the original stamp is aiming at whatever now happens to
    sit at that number. On a page with a modal over it that is usually the
    modal's own button.
    """
    if ctx.element is None:
        return None

    wanted = _signature(ctx.element)
    match = next((item for item in state.elements if _signature(item) == wanted), None)
    return selector_for(match) if match is not None else None


def _signature(element: ElementInfo) -> tuple[str, str, str]:
    """What identifies a control across a re-render.

    Not the index, which perception assigns fresh each time, and not the id,
    which is the thing that gets shuffled.
    """
    return (element.role or TAG_ROLES.get(element.tag, ""), element.name, element.text)


class ReperceiveByIndex:
    """Read the page again and find the same control by what it is.

    The commonest failure by a distance: the page re-rendered between being
    perceived and being clicked, so the stamped index now points at something
    else or at nothing.
    """

    name = "reperceive_by_index"
    label = "re-reading the page"

    def applicable(self, ctx: RecoveryContext) -> bool:
        return ctx.targets_an_element and not blocked_by_overlay(ctx)

    async def attempt(self, ctx: RecoveryContext) -> AttemptResult:
        assert ctx.element is not None
        fresh = await ctx.browser.perceive()
        selector = relocate(ctx, fresh)
        if selector is None:
            return AttemptResult(RecoveryOutcome.SKIPPED, error="The control is not on the page.")

        return await retry_with(ctx, selector)


class RoleNameLocator:
    """Ask for it the way a screen reader would."""

    name = "role_name_locator"
    label = "re-locating by name"

    def applicable(self, ctx: RecoveryContext) -> bool:
        if not ctx.targets_an_element or blocked_by_overlay(ctx):
            return False
        assert ctx.element is not None
        return bool(ctx.element.name) and bool(self._role(ctx.element))

    async def attempt(self, ctx: RecoveryContext) -> AttemptResult:
        assert ctx.element is not None
        role = self._role(ctx.element)
        escaped = ctx.element.name.replace('"', '\\"')
        return await retry_with(ctx, f'role={role}[name="{escaped}"]')

    @staticmethod
    def _role(element: ElementInfo) -> str:
        return element.role or TAG_ROLES.get(element.tag, "")


class TextLocator:
    """Find it by what it says: exactly, then loosely, then approximately."""

    name = "text_locator"
    label = "matching its text"

    def applicable(self, ctx: RecoveryContext) -> bool:
        if not ctx.targets_an_element or blocked_by_overlay(ctx):
            return False
        assert ctx.element is not None
        return bool((ctx.element.name or ctx.element.text).strip())

    async def attempt(self, ctx: RecoveryContext) -> AttemptResult:
        assert ctx.element is not None
        wanted = (ctx.element.name or ctx.element.text).strip()
        escaped = wanted.replace('"', '\\"')

        # Quoted is exact and case-sensitive; unquoted is a case-insensitive
        # substring. Trying both costs one extra attempt and catches a label
        # that has been recapitalised.
        for selector in (f'text="{escaped}"', f"text={escaped}"):
            attempt = await retry_with(ctx, selector)
            if attempt.succeeded:
                return attempt

        return await self._fuzzy(ctx, wanted)

    async def _fuzzy(self, ctx: RecoveryContext, wanted: str) -> AttemptResult:
        """Nearest label on the page, if it is near enough.

        For a button that has been reworded rather than renamed: "Search
        flights" becoming "Find flights" is the layout-B chaos case.
        """
        fresh = await ctx.browser.perceive()
        best: tuple[float, ElementInfo] | None = None

        for item in fresh.elements:
            label = (item.name or item.text).strip()
            if not label:
                continue
            ratio = difflib.SequenceMatcher(None, wanted.casefold(), label.casefold()).ratio()
            if ratio >= FUZZY_THRESHOLD and (best is None or ratio > best[0]):
                best = (ratio, item)

        if best is None:
            return AttemptResult(RecoveryOutcome.SKIPPED, error="Nothing on the page is close.")

        selector = selector_for(best[1])
        assert selector is not None
        logger.info("recovery_fuzzy_match", wanted=wanted, found=best[1].name, ratio=best[0])
        return await retry_with(ctx, selector)


class StoredSelector:
    """Use the path captured when the page was first read."""

    name = "stored_selector"
    label = "the stored selector"

    def applicable(self, ctx: RecoveryContext) -> bool:
        if not ctx.targets_an_element or blocked_by_overlay(ctx):
            return False
        assert ctx.element is not None
        return bool(ctx.element.css_path or ctx.element.xpath)

    async def attempt(self, ctx: RecoveryContext) -> AttemptResult:
        assert ctx.element is not None
        for selector in self._candidates(ctx.element):
            attempt = await retry_with(ctx, selector)
            if attempt.succeeded:
                return attempt
        return AttemptResult(RecoveryOutcome.FAILED, error="The stored path no longer resolves.")

    @staticmethod
    def _candidates(element: ElementInfo) -> list[str]:
        found = []
        if element.css_path:
            found.append(element.css_path)
        if element.xpath:
            found.append(f"xpath={element.xpath}")
        return found


class ScrollAndWait:
    """Bring it on screen first.

    Playwright scrolls before clicking, so this is not about the click itself:
    it is about a page that renders more content as you reach it, where the
    control only becomes real once something has scrolled near it.
    """

    name = "scroll_and_wait"
    label = "scrolling to it"

    def applicable(self, ctx: RecoveryContext) -> bool:
        return ctx.targets_an_element

    async def attempt(self, ctx: RecoveryContext) -> AttemptResult:
        assert ctx.element is not None
        selector = selector_for(ctx.element)
        assert selector is not None

        found = await ctx.browser.scroll_into_view(selector)
        if not found:
            for candidate in StoredSelector._candidates(ctx.element):
                if await ctx.browser.scroll_into_view(candidate):
                    found = True
                    selector = candidate
                    break

        if not found:
            return AttemptResult(RecoveryOutcome.SKIPPED, error="Could not scroll to it.")

        await ctx.browser.settle()
        return await retry_with(ctx, selector)


class DismissOverlay:
    """Close whatever is sitting on top of the page, then try again."""

    name = "dismiss_overlay"
    label = "closing a popup"

    def applicable(self, ctx: RecoveryContext) -> bool:
        if not ctx.targets_an_element:
            return False
        return bool(self.closers(ctx.after)) or blocked_by_overlay(ctx)

    async def attempt(self, ctx: RecoveryContext) -> AttemptResult:
        # Read the page as it is now. The overlay may have arrived after the
        # state that failed was captured, which is the popup chaos case, and
        # then it is not in `ctx.after` at all.
        # Close every overlay, not just the first. The chaos combination of a
        # cookie banner and a newsletter popup puts two of them on the page,
        # and clearing one still leaves the click blocked.
        closed = 0
        current = await ctx.browser.perceive()

        for _ in range(MAX_OVERLAYS):
            closers = self.closers(current)
            if not closers:
                break

            selector = selector_for(closers[0])
            if selector is None:
                break
            try:
                await ctx.browser.execute(
                    build_action(_click_like(ctx.decision), closers[0], selector=selector)
                )
            except Exception:
                break

            closed += 1
            logger.info("recovery_overlay_dismissed", label=closers[0].name)
            await ctx.browser.settle()
            current = await ctx.browser.perceive()

        if not closed:
            # Escape closes a well-built modal and does nothing to a page
            # without one, so it is safe to try either way.
            await ctx.browser.press("Escape")

        await ctx.browser.settle()
        ctx.after = await ctx.browser.perceive()

        selector = relocate(ctx, ctx.after)
        if selector is None:
            return AttemptResult(
                RecoveryOutcome.SKIPPED, error="The control is still not reachable."
            )
        return await retry_with(ctx, selector)

    @staticmethod
    def closers(state: PageState) -> list[ElementInfo]:
        """Controls that look like they close something.

        Matched on the whole label rather than a substring, so "Accept" does
        not also match "Accept and book this flight", which is a button that
        spends money.
        """
        found: list[tuple[int, ElementInfo]] = []
        for element in state.elements:
            label = (element.name or element.text).strip().casefold()
            if not label or element.tag not in {"button", "a"}:
                continue
            for rank, wanted in enumerate(OVERLAY_LABELS):
                if label == wanted:
                    found.append((rank, element))
                    break

        found.sort(key=lambda pair: pair[0])
        labelled = [element for _, element in found]
        # An icon with no label is the last resort: a position is a guess.
        return labelled + [e for e in icon_closers(state) if e not in labelled]


class VisualGrounding:
    """Look at the pixels, then click where they say (spec section 4.8, rung 7).

    The last rung for a reason: it costs a screenshot, a vision call and a
    second or two, and it is the only one that can act on a target no selector
    can name. Clicking a coordinate skips every guard that a selector goes
    through, so it runs after everything that works by name has failed.
    """

    name = "visual_grounding"
    label = "looking at the screenshot"

    def __init__(self, grounder: Grounder | None = None) -> None:
        self._grounder = grounder

    def applicable(self, ctx: RecoveryContext) -> bool:
        # Without a grounder this is the placeholder it used to be, and a rung
        # that always reported success would end the ladder before the rungs
        # below it ran.
        return self._grounder is not None and ctx.targets_an_element

    async def attempt(self, ctx: RecoveryContext) -> AttemptResult:
        if self._grounder is None:
            return AttemptResult(
                RecoveryOutcome.SKIPPED, error="Visual grounding is not configured."
            )

        shot = await ctx.browser.screenshot()
        current = await ctx.browser.perceive()
        outcome = await self._grounder.resolve(
            ctx.decision, current, shot.jpeg_bytes, "the ladder ran out of names"
        )

        if outcome.error is not None or outcome.point is None:
            return AttemptResult(
                RecoveryOutcome.SKIPPED,
                error=outcome.error or "Vision could not find the target.",
            )

        baseline = current
        x, y = outcome.point
        if not await ctx.browser.click_at(x, y):
            return AttemptResult(
                RecoveryOutcome.FAILED, error=f"The point ({x}, {y}) is off screen."
            )

        await ctx.browser.settle()
        after = await ctx.browser.perceive()
        ctx.after = after

        # Still the verifier's call. A click landing somewhere is not the same
        # as the step having worked, and that is truer here than anywhere: a
        # coordinate click always "succeeds" as far as the mouse is concerned.
        verdict = await ctx.verifier.check(ctx.decision, baseline, after, ctx.result)
        outcome_kind = RecoveryOutcome.SUCCEEDED if verdict.ok else RecoveryOutcome.FAILED
        return AttemptResult(
            outcome_kind, verdict=verdict, error=None if verdict.ok else verdict.detail
        )


def _click_like(decision: Decision) -> Decision:
    """The same decision as a plain click, for pressing a close button."""
    return decision.model_copy(update={"action": ActionType.CLICK, "value": None})


ORDERED_STRATEGIES: Final[tuple[RecoveryStrategy, ...]] = (
    ReperceiveByIndex(),
    RoleNameLocator(),
    TextLocator(),
    StoredSelector(),
    ScrollAndWait(),
    DismissOverlay(),
    VisualGrounding(),
)


def strategies_with_grounding(grounder: Grounder) -> tuple[RecoveryStrategy, ...]:
    """The ladder with rung seven able to look at the page.

    Same order; the last rung just has something behind it. Without a grounder
    it stays not-applicable, which is what keeps a run with no vision provider
    from ending the ladder on a rung that cannot do anything.
    """
    return (*ORDERED_STRATEGIES[:-1], VisualGrounding(grounder))
