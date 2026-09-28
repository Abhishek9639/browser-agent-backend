"""The recovery ladder (spec section 4.8).

Two halves. The runner's ordering, budget and reporting are tested against
fake strategies, because that logic is about bookkeeping. The strategies
themselves are tested against real pages in a real browser, because each one
exists to beat a specific way a page can break and a fake page cannot break
that way.
"""

from typing import Any

import pytest

from app.agent.browser.agent_browser import DriverBrowser
from app.agent.browser.driver import ActionResult, PlaywrightDriver
from app.agent.browser.errors import ActionErrorCode
from app.agent.contracts import Attempt, AttemptResult, RecoveryContext, Verdict
from app.agent.perception.dom_extractor import BoundingBox, ElementInfo
from app.agent.perception.perceive import perceive
from app.agent.perception.state import PageState
from app.agent.planner.schema import Decision
from app.agent.recovery.routing import order_for, route_for
from app.agent.recovery.runner import RecoveryRunner, failure_sentence
from app.agent.recovery.strategies import (
    ORDERED_STRATEGIES,
    DismissOverlay,
    ReperceiveByIndex,
    RoleNameLocator,
    ScrollAndWait,
    StoredSelector,
    TextLocator,
    VisualGrounding,
)
from app.agent.verification.verifier import DeterministicVerifier
from app.core.config import Settings
from app.domain.enums import RecoveryOutcome, VerificationMethod

# ------------------------------------------------------------- the runner


def element(
    index: int = 0,
    *,
    name: str = "Search flights",
    tag: str = "button",
    in_viewport: bool = True,
) -> ElementInfo:
    return ElementInfo(
        index=index,
        tag=tag,
        name=name,
        css_path="#search",
        bbox=BoundingBox(x=0, y=0, width=80, height=24),
        in_viewport=in_viewport,
    )


def context(**overrides: Any) -> RecoveryContext:
    decision = Decision.model_validate(
        {
            "action": "click",
            "targetIndex": 0,
            "target": "Search flights",
            "reasoning": "run it",
            "expectedOutcome": {"type": "dom_changed", "value": ""},
            "confidence": 0.9,
        }
    )
    state = PageState(url="https://skyfare.test/", elements=[element()])
    base: dict[str, Any] = {
        "decision": decision,
        "element": element(),
        "before": state,
        "after": state,
        "result": ActionResult(ok=True, duration_ms=1, url_before="a", url_after="a"),
        "verdict": Verdict(ok=False, method=VerificationMethod.DOM_CHANGED),
        "browser": None,
        "verifier": None,
    }
    base.update(overrides)
    return RecoveryContext(**base)


class Fake:
    """A strategy with a scripted outcome."""

    def __init__(self, name: str, outcome: RecoveryOutcome, *, applies: bool = True) -> None:
        self.name = name
        self.label = f"trying {name}"
        self._outcome = outcome
        self._applies = applies
        self.attempts = 0

    def applicable(self, ctx: RecoveryContext) -> bool:
        return self._applies

    async def attempt(self, ctx: RecoveryContext) -> AttemptResult:
        self.attempts += 1
        verdict = Verdict(
            ok=self._outcome is RecoveryOutcome.SUCCEEDED, method=VerificationMethod.DOM_CHANGED
        )
        return AttemptResult(self._outcome, verdict=verdict if verdict.ok else None)


async def test_the_first_strategy_that_works_ends_the_ladder() -> None:
    first = Fake("a", RecoveryOutcome.FAILED)
    second = Fake("b", RecoveryOutcome.SUCCEEDED)
    third = Fake("c", RecoveryOutcome.SUCCEEDED)
    ctx = context()

    verdict = await RecoveryRunner(strategies=(first, second, third)).run(ctx)

    assert verdict is not None and verdict.ok
    assert (first.attempts, second.attempts, third.attempts) == (1, 1, 0)


async def test_strategies_are_tried_in_order() -> None:
    rungs = tuple(Fake(name, RecoveryOutcome.FAILED) for name in "abc")
    ctx = context()

    await RecoveryRunner(max_attempts=9, strategies=rungs).run(ctx)

    assert [a.strategy for a in ctx.attempts] == ["a", "b", "c"]
    assert [a.number for a in ctx.attempts] == [1, 2, 3]


async def test_the_retry_budget_is_respected() -> None:
    rungs = tuple(Fake(name, RecoveryOutcome.FAILED) for name in "abcdefg")
    ctx = context()

    verdict = await RecoveryRunner(max_attempts=3, strategies=rungs).run(ctx)

    # Without a budget, a seven rung ladder on a page that simply does not
    # have the button turns one failed step into seven.
    assert verdict is None
    assert len(ctx.attempts) == 3
    assert sum(rung.attempts for rung in rungs) == 3


async def test_a_strategy_that_does_not_apply_is_skipped_for_free() -> None:
    skipped = Fake("a", RecoveryOutcome.FAILED, applies=False)
    tried = Fake("b", RecoveryOutcome.FAILED)
    ctx = context()

    await RecoveryRunner(max_attempts=1, strategies=(skipped, tried)).run(ctx)

    # Not applicable means it never ran, so it must not cost a retry.
    assert skipped.attempts == 0
    assert tried.attempts == 1
    assert [a.strategy for a in ctx.attempts] == ["b"]


async def test_a_skipped_outcome_does_not_spend_the_budget() -> None:
    class Skips(Fake):
        async def attempt(self, ctx: RecoveryContext) -> AttemptResult:
            self.attempts += 1
            return AttemptResult(RecoveryOutcome.SKIPPED, error="nothing to do")

    skipper = Skips("a", RecoveryOutcome.SKIPPED)
    worker = Fake("b", RecoveryOutcome.SUCCEEDED)
    ctx = context()

    verdict = await RecoveryRunner(max_attempts=1, strategies=(skipper, worker)).run(ctx)

    assert verdict is not None
    assert worker.attempts == 1


async def test_a_strategy_that_throws_does_not_stop_the_ladder() -> None:
    class Explodes(Fake):
        async def attempt(self, ctx: RecoveryContext) -> AttemptResult:
            raise RuntimeError("playwright fell over")

    boom = Explodes("a", RecoveryOutcome.FAILED)
    rescue = Fake("b", RecoveryOutcome.SUCCEEDED)
    ctx = context()

    verdict = await RecoveryRunner(strategies=(boom, rescue)).run(ctx)

    assert verdict is not None and verdict.ok
    assert ctx.attempts[0].outcome is RecoveryOutcome.FAILED
    assert ctx.attempts[0].error == "RuntimeError"


async def test_every_attempt_is_announced_as_it_happens() -> None:
    seen: list[Attempt] = []

    async def listen(attempt: Attempt) -> None:
        seen.append(attempt)

    ctx = context(on_attempt=listen)
    rungs = tuple(Fake(name, RecoveryOutcome.FAILED) for name in "ab")

    await RecoveryRunner(max_attempts=5, strategies=rungs).run(ctx)

    # A ladder can take several seconds, and the live view exists to show a
    # person what the agent is doing rather than to pause.
    assert [a.strategy for a in seen] == ["a", "b"]


async def test_nothing_applicable_means_nothing_was_attempted() -> None:
    ctx = context()

    verdict = await RecoveryRunner(
        strategies=(Fake("a", RecoveryOutcome.FAILED, applies=False),)
    ).run(ctx)

    assert verdict is None
    assert ctx.attempts == []


# --------------------------------------------------------- failure reason


def test_the_failure_sentence_names_the_action_target_and_rungs() -> None:
    ctx = context()

    sentence = failure_sentence(ctx, ["re-reading the page", "matching its text"])

    assert sentence == (
        "Could not click 'Search flights': tried re-reading the page and matching its text."
    )


def test_the_failure_sentence_handles_one_rung() -> None:
    assert failure_sentence(context(), ["scrolling to it"]) == (
        "Could not click 'Search flights': tried scrolling to it."
    )


def test_the_failure_sentence_without_any_rungs() -> None:
    assert failure_sentence(context(), []) == "Could not click 'Search flights'."


def test_the_failure_sentence_does_not_repeat_a_rung() -> None:
    sentence = failure_sentence(context(), ["scrolling to it", "scrolling to it"])

    assert sentence.count("scrolling to it") == 1


# ------------------------------------------------------- applicability


def test_visual_grounding_is_not_applicable_until_it_exists() -> None:
    # A rung that always reported success would end the ladder before the
    # rungs below it ever ran.
    assert VisualGrounding().applicable(context()) is False


def test_the_ladder_is_in_the_spec_order() -> None:
    assert [type(rung) for rung in ORDERED_STRATEGIES] == [
        ReperceiveByIndex,
        RoleNameLocator,
        TextLocator,
        StoredSelector,
        ScrollAndWait,
        DismissOverlay,
        VisualGrounding,
    ]


def test_nothing_applies_to_an_action_with_no_element() -> None:
    ctx = context(
        element=None,
        decision=Decision.model_validate(
            {
                "action": "navigate",
                "value": "https://skyfare.test/",
                "reasoning": "go",
                "expectedOutcome": {"type": "url_contains", "value": "skyfare"},
                "confidence": 0.9,
            }
        ),
    )

    # Navigating somewhere again is not recovery, it is the same request twice.
    assert not [rung for rung in ORDERED_STRATEGIES if rung.applicable(ctx)]


# ------------------------------------------ strategies against real pages


pytestmark_browser = pytest.mark.browser


@pytest.fixture
async def browser(driver: PlaywrightDriver, browser_settings: Settings) -> DriverBrowser:
    return DriverBrowser(driver, browser_settings)


def click_for(target: ElementInfo) -> Decision:
    return Decision.model_validate(
        {
            "action": "click",
            "targetIndex": target.index,
            "target": target.name,
            "reasoning": "click it",
            "expectedOutcome": {"type": "dom_changed", "value": ""},
            "confidence": 0.9,
        }
    )


async def live_context(
    browser: DriverBrowser, driver: PlaywrightDriver, html_server: str, page: str, label: str
) -> RecoveryContext:
    """Perceive a page, pick a control by name, and pretend the click failed."""
    await driver.page.goto(f"{html_server}/{page}")
    state = await perceive(driver.page, driver)
    target = next(item for item in state.elements if label in item.name)

    return RecoveryContext(
        decision=click_for(target),
        element=target,
        before=state,
        after=state,
        result=ActionResult(ok=False, duration_ms=1, url_before="", url_after=""),
        verdict=Verdict(ok=False, method=VerificationMethod.DOM_CHANGED),
        browser=browser,
        verifier=DeterministicVerifier(),
    )


@pytest.mark.browser
async def test_a_rerendered_page_is_beaten_by_reading_it_again(
    browser: DriverBrowser, driver: PlaywrightDriver, html_server: str
) -> None:
    ctx = await live_context(browser, driver, html_server, "recovery_rerender.html", "Book AI-812")
    # The page rebuilds its rows, which drops the stamps the indices point at.
    await driver.page.wait_for_timeout(400)

    result = await ReperceiveByIndex().attempt(ctx)

    assert result.succeeded, result.error
    assert "clicked Book AI-812" in await driver.page.inner_text("#out")


@pytest.mark.browser
async def test_an_overlay_is_beaten_by_closing_it(
    browser: DriverBrowser, driver: PlaywrightDriver, html_server: str
) -> None:
    ctx = await live_context(browser, driver, html_server, "recovery_overlay.html", "Search")
    # The banner arrives after the page was read, so the agent planned
    # against a button it could see and then could not reach.
    await driver.page.wait_for_selector("#veil")

    result = await DismissOverlay().attempt(ctx)

    assert result.succeeded, result.error
    assert await driver.page.inner_text("#out") == "searched"
    assert await driver.page.locator("#veil").count() == 0


@pytest.mark.browser
async def test_the_least_consenting_close_button_is_the_one_used(
    browser: DriverBrowser, driver: PlaywrightDriver, html_server: str
) -> None:
    ctx = await live_context(browser, driver, html_server, "recovery_overlay.html", "Search")
    await driver.page.wait_for_selector("#veil")

    await DismissOverlay().attempt(ctx)

    # "Reject non-essential" outranks "Accept all cookies": closing a banner
    # to get on with the task must not consent to anything on the user's
    # behalf.
    assert await driver.page.evaluate("() => document.body.dataset.closedBy") == "reject"


@pytest.mark.browser
async def test_an_element_below_the_fold_is_beaten_by_scrolling(
    browser: DriverBrowser, driver: PlaywrightDriver, html_server: str
) -> None:
    ctx = await live_context(browser, driver, html_server, "recovery_belowfold.html", "Search")

    result = await ScrollAndWait().attempt(ctx)

    assert result.succeeded, result.error
    assert await driver.page.inner_text("#out") == "searched"


@pytest.mark.browser
async def test_renamed_ids_are_beaten_by_the_role_and_name(
    browser: DriverBrowser, driver: PlaywrightDriver, html_server: str
) -> None:
    ctx = await live_context(
        browser, driver, html_server, "recovery_renamed.html", "Search flights"
    )
    # Every id changes, which is the shuffle chaos case. The button is still a
    # button called "Search flights".
    await driver.page.evaluate(
        "() => { document.querySelectorAll('[id]').forEach("
        "(node) => { node.id = 'x' + Math.random().toString(36).slice(2); }); }"
    )

    result = await RoleNameLocator().attempt(ctx)

    assert result.succeeded, result.error
    assert await driver.page.inner_text("p#out, p") != ""


@pytest.mark.browser
async def test_the_stored_path_still_resolves_after_the_stamps_are_stripped(
    browser: DriverBrowser, driver: PlaywrightDriver, html_server: str
) -> None:
    ctx = await live_context(browser, driver, html_server, "simple_form.html", "Search flights")
    await driver.page.evaluate(
        "() => { document.querySelectorAll('[data-agent-idx]').forEach("
        "(node) => node.removeAttribute('data-agent-idx')); }"
    )

    result = await StoredSelector().attempt(ctx)

    assert result.succeeded, result.error


@pytest.mark.browser
async def test_a_reworded_button_is_beaten_by_a_fuzzy_match(
    browser: DriverBrowser, driver: PlaywrightDriver, html_server: str
) -> None:
    ctx = await live_context(browser, driver, html_server, "simple_form.html", "Search flights")
    # Layout B renames the button. Same control, different words.
    await driver.page.evaluate(
        "() => { document.getElementById('submit').textContent = 'Search flight'; }"
    )

    result = await TextLocator().attempt(ctx)

    assert result.succeeded, result.error


# ------------------------------------------------------- routing by error code


def failed_with(code: ActionErrorCode | None, *, in_viewport: bool = True) -> RecoveryContext:
    """A failed step whose error code is the only interesting thing about it."""
    target = element(in_viewport=in_viewport)
    return context(
        element=target,
        after=PageState(url="https://skyfare.test/", elements=[target]),
        result=ActionResult(
            ok=False,
            duration_ms=10_000,
            url_before="https://skyfare.test/",
            url_after="https://skyfare.test/",
            error_code=code,
        ),
    )


def names(strategies: tuple[Any, ...]) -> list[str]:
    return [item.name for item in strategies]


def test_a_missing_element_goes_to_the_relocate_rungs_first() -> None:
    ordered = order_for(ORDERED_STRATEGIES, failed_with(ActionErrorCode.ELEMENT_NOT_FOUND))

    assert names(ordered)[:4] == [
        "reperceive_by_index",
        "role_name_locator",
        "text_locator",
        "stored_selector",
    ]


def test_a_timeout_on_a_visible_element_goes_to_the_overlay_rung_first() -> None:
    """The case this exists for.

    A cookie banner over the button fails the click with a timeout. Re-locating
    finds the same covered button three times and waits out the ten second
    action timeout each time: thirty seconds spent to not dismiss a popup.
    """
    ordered = order_for(ORDERED_STRATEGIES, failed_with(ActionErrorCode.TIMEOUT))

    assert names(ordered)[:2] == ["dismiss_overlay", "scroll_and_wait"]


def test_a_timeout_on_an_offscreen_element_scrolls_first() -> None:
    """Scrolling is cheaper than hunting for a popup that may not be there."""
    ordered = order_for(ORDERED_STRATEGIES, failed_with(ActionErrorCode.TIMEOUT, in_viewport=False))

    assert names(ordered)[:2] == ["scroll_and_wait", "dismiss_overlay"]


def test_an_unresponsive_element_is_routed_like_a_timeout() -> None:
    ordered = order_for(ORDERED_STRATEGIES, failed_with(ActionErrorCode.NOT_INTERACTABLE))

    assert names(ordered)[:1] == ["dismiss_overlay"]


def test_routing_reorders_and_never_drops_a_rung() -> None:
    """A misclassified failure must still be able to reach every rung.

    The error code comes from reading a message Playwright wrote, so it is a
    hint rather than a diagnosis. Truncating the ladder on a hint would turn a
    wrong guess into an unrecoverable step.
    """
    ordered = order_for(ORDERED_STRATEGIES, failed_with(ActionErrorCode.TIMEOUT))

    assert sorted(names(ordered)) == sorted(names(ORDERED_STRATEGIES))


def test_a_verification_failure_leaves_the_order_alone() -> None:
    """The action worked and the outcome was wrong, so no locator is at fault."""
    ordered = order_for(ORDERED_STRATEGIES, failed_with(None))

    assert names(ordered) == names(ORDERED_STRATEGIES)


def test_a_navigation_failure_has_no_route() -> None:
    """Nothing in the ladder re-locates a page that would not load."""
    assert route_for(failed_with(ActionErrorCode.NAVIGATION_FAILED)) == ()


@pytest.mark.browser
async def test_the_ladder_dismisses_the_overlay_on_its_first_attempt(
    browser: DriverBrowser, driver: PlaywrightDriver, html_server: str
) -> None:
    """End to end, against a real banner over a real button.

    Asserts the attempt log rather than the wall clock. The saving is real, but
    a timing assertion is how a suite becomes flaky on a loaded machine.
    """
    ctx = await live_context(browser, driver, html_server, "recovery_overlay.html", "Search")
    await driver.page.wait_for_selector("#veil")
    # What the loop would have: the page as it was when the action failed,
    # banner and all, rather than the page the decision was made against.
    ctx.after = await browser.perceive()
    ctx.result = ActionResult(
        ok=False,
        duration_ms=10_000,
        url_before=ctx.after.url,
        url_after=ctx.after.url,
        error_code=ActionErrorCode.TIMEOUT,
    )

    verdict = await RecoveryRunner(max_attempts=3).run(ctx)

    assert verdict is not None and verdict.ok
    assert ctx.attempts[0].strategy == "dismiss_overlay"
    assert len(ctx.attempts) == 1
