"""Deterministic verification (spec section 4.7, ADR-004).

The model proposes actions. Nothing here asks it whether they worked.

That separation is the point. A model that is wrong about what a click did is
exactly the model that will confidently say it worked, so letting it mark its
own homework turns one error into a silent one. Everything below is a
comparison between two page states, and every comparison is reproducible from
the two rows that get stored.

The section 4.5 schema gives the planner six outcome types to declare. This
runs a few more checks than that, because the defaults need them: a scroll has
no outcome type of its own, and neither does "the extract came back empty".
The precise check is recorded in ``actual["check"]`` and the stored method
stays one of the six, so the enum column does not have to grow to hold a
detail that belongs in JSON.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final
from urllib.parse import urlsplit

from app.agent.browser.driver import ActionResult
from app.agent.contracts import Verdict
from app.agent.perception.dom_extractor import ElementInfo
from app.agent.perception.state import PageState
from app.agent.planner.schema import Decision, ExpectedOutcome
from app.core.logging import get_logger
from app.domain.enums import ActionType, VerificationMethod

__all__ = ["STORED_AS", "Check", "DeterministicVerifier"]

logger = get_logger(__name__)


class Check(StrEnum):
    """What was actually compared.

    Wider than :class:`VerificationMethod`, which is the storage contract.
    """

    URL_CONTAINS = "url_contains"
    URL_CHANGED = "url_changed"
    ELEMENT_VISIBLE = "element_visible"
    TEXT_PRESENT = "text_present"
    VALUE_EQUALS = "value_equals"
    DOM_CHANGED = "dom_changed"
    NEW_TAB_OPENED = "new_tab_opened"
    ELEMENT_STATE = "element_state"
    SCROLLED = "scrolled"
    EXTRACTED = "extracted"
    NOTHING_TO_CHECK = "nothing_to_check"


# Which of the six storable methods each check is recorded as. Spec section 4.7
# writes "URL change/contains" as one item, so both URL checks store as
# url_contains. A control's checked or selected state is its value, so
# element_state stores as value_equals. A scroll leaves the DOM the same but
# the page state different, which is the bucket dom_changed already covers.
STORED_AS: Final[dict[Check, VerificationMethod]] = {
    Check.URL_CONTAINS: VerificationMethod.URL_CONTAINS,
    Check.URL_CHANGED: VerificationMethod.URL_CONTAINS,
    Check.ELEMENT_VISIBLE: VerificationMethod.ELEMENT_VISIBLE,
    Check.TEXT_PRESENT: VerificationMethod.TEXT_PRESENT,
    Check.VALUE_EQUALS: VerificationMethod.VALUE_EQUALS,
    Check.DOM_CHANGED: VerificationMethod.DOM_CHANGED,
    Check.NEW_TAB_OPENED: VerificationMethod.NEW_TAB_OPENED,
    Check.ELEMENT_STATE: VerificationMethod.VALUE_EQUALS,
    Check.SCROLLED: VerificationMethod.DOM_CHANGED,
    Check.EXTRACTED: VerificationMethod.TEXT_PRESENT,
    Check.NOTHING_TO_CHECK: VerificationMethod.DOM_CHANGED,
}

# Checks whose declared expectation is useless without a value to compare
# against. A "text_present" with nothing to look for passes on any page.
NEEDS_VALUE: Final[frozenset[Check]] = frozenset(
    {Check.URL_CONTAINS, Check.TEXT_PRESENT, Check.ELEMENT_VISIBLE}
)


@dataclass(frozen=True, slots=True)
class _Outcome:
    """One check's result, before it is dressed up as a Verdict."""

    ok: bool
    check: Check
    actual: dict[str, Any]
    detail: str


class DeterministicVerifier:
    """Compares two page states. Never calls a model."""

    async def check(
        self,
        decision: Decision,
        before: PageState,
        after: PageState,
        result: ActionResult,
    ) -> Verdict:
        expectation = self._expectation(decision)
        outcome = self._run(expectation, decision, before, after, result)

        logger.debug(
            "verified",
            action=decision.action.value,
            check=outcome.check.value,
            ok=outcome.ok,
        )
        return Verdict(
            ok=outcome.ok,
            method=STORED_AS[outcome.check],
            expected={
                "type": expectation.type.value,
                "value": expectation.value,
                "check": outcome.check.value,
            },
            actual=outcome.actual,
            detail=outcome.detail,
        )

    # ------------------------------------------------------- what to check

    def _expectation(self, decision: Decision) -> ExpectedOutcome:
        """The planner's expectation, or the default for the action.

        The schema makes ``expectedOutcome`` required, so there is always
        something declared. It is not always usable: a model that asks for
        ``text_present`` with an empty value has declared a check that passes
        on every page in the world, and treating that as verification is worse
        than having none.
        """
        declared = decision.expected_outcome
        check = _as_check(declared.type)

        if check in NEEDS_VALUE and not declared.value.strip():
            return self._default_for(decision)
        return declared

    def _default_for(self, decision: Decision) -> ExpectedOutcome:
        """What counts as success when the planner did not say usefully."""
        match decision.action:
            case ActionType.NAVIGATE | ActionType.NEW_TAB:
                host = urlsplit(decision.value or "").netloc
                if host:
                    return ExpectedOutcome(type=VerificationMethod.URL_CONTAINS, value=host)
                return ExpectedOutcome(type=VerificationMethod.URL_CONTAINS, value="")
            case ActionType.GO_BACK:
                # Going back has one observable effect. An empty url_contains
                # becomes url_changed further down, which is the check that
                # actually answers "did we leave that page".
                return ExpectedOutcome(type=VerificationMethod.URL_CONTAINS, value="")
            case ActionType.TYPE | ActionType.SELECT:
                return ExpectedOutcome(
                    type=VerificationMethod.VALUE_EQUALS, value=decision.value or ""
                )
            case _:
                return ExpectedOutcome(type=VerificationMethod.DOM_CHANGED, value="")

    # ------------------------------------------------------------ checking

    def _run(
        self,
        expectation: ExpectedOutcome,
        decision: Decision,
        before: PageState,
        after: PageState,
        result: ActionResult,
    ) -> _Outcome:
        # A few actions are verified by what they are, not by what was asked
        # for. Their defaults have no outcome type of their own.
        if decision.action is ActionType.DONE:
            return _Outcome(True, Check.NOTHING_TO_CHECK, {"action": "done"}, "Nothing to check.")
        if decision.action is ActionType.EXTRACT:
            return _extracted(result)
        if decision.action is ActionType.SCROLL:
            return _scrolled(before, after)
        if decision.action is ActionType.CLICK and _is_stateful(decision, before):
            # Clicking a disclosure flips aria-expanded and nothing else, and
            # `expanded` is not part of the DOM hash, so dom_changed would call
            # a working click a failure.
            return _element_state(decision, before, after, expectation.value)

        check = _as_check(expectation.type)
        if check is Check.URL_CONTAINS and not expectation.value.strip():
            check = Check.URL_CHANGED

        match check:
            case Check.URL_CONTAINS:
                return _url_contains(after, expectation.value)
            case Check.URL_CHANGED:
                return _url_changed(before, after)
            case Check.ELEMENT_VISIBLE:
                return _element_visible(after, expectation.value)
            case Check.TEXT_PRESENT:
                return _text_present(after, expectation.value)
            case Check.VALUE_EQUALS:
                return _value_equals(decision, before, after, expectation.value)
            case Check.NEW_TAB_OPENED:
                return _new_tab_opened(before, after)
            case _:
                return _dom_changed(before, after)


# ----------------------------------------------------------------- checks


def _url_contains(after: PageState, wanted: str) -> _Outcome:
    hit = wanted.casefold() in after.url.casefold()
    return _Outcome(
        hit,
        Check.URL_CONTAINS,
        {"check": Check.URL_CONTAINS.value, "url": after.url},
        f"URL {'contains' if hit else 'does not contain'} {wanted!r}.",
    )


def _url_changed(before: PageState, after: PageState) -> _Outcome:
    changed = before.url != after.url
    return _Outcome(
        changed,
        Check.URL_CHANGED,
        {"check": Check.URL_CHANGED.value, "before": before.url, "after": after.url},
        "The URL changed." if changed else "The URL did not change.",
    )


def _element_visible(after: PageState, wanted: str) -> _Outcome:
    match = _find(after, wanted)
    return _Outcome(
        match is not None,
        Check.ELEMENT_VISIBLE,
        {
            "check": Check.ELEMENT_VISIBLE.value,
            "found": match.name if match else None,
            "index": match.index if match else None,
        },
        f"{wanted!r} {'is' if match else 'is not'} on the page.",
    )


def _text_present(after: PageState, wanted: str) -> _Outcome:
    needle = wanted.casefold()
    # The page's own text first. Searching only the interactive elements would
    # miss a result rendered into a paragraph, which is most of what a page
    # says: "Searched: Delhi" is not a button.
    hit = needle in after.text.casefold() or needle in after.summary_text.casefold()
    if not hit:
        hit = any(
            needle in f"{element.name} {element.text}".casefold() for element in after.elements
        )
    return _Outcome(
        hit,
        Check.TEXT_PRESENT,
        {"check": Check.TEXT_PRESENT.value, "searched": wanted},
        f"{wanted!r} {'appears' if hit else 'does not appear'} on the page.",
    )


def _value_equals(decision: Decision, before: PageState, after: PageState, wanted: str) -> _Outcome:
    """Re-read the field and compare.

    The element is found again by identity rather than by index, because the
    page was perceived a second time after the action and the indices are
    assigned fresh each time.
    """
    target = _same_element(decision, before, after)
    if target is None:
        return _Outcome(
            False,
            Check.VALUE_EQUALS,
            {"check": Check.VALUE_EQUALS.value, "found": None},
            "The field is no longer on the page.",
        )

    if target.is_masked:
        # The collector masks secrets at source, so the real value is not here
        # to compare and never will be. Having the field is the evidence.
        return _Outcome(
            True,
            Check.VALUE_EQUALS,
            {"check": Check.VALUE_EQUALS.value, "masked": True},
            "The field holds a masked value, which cannot be compared.",
        )

    actual = (target.value or "").strip()
    ok = actual.casefold() == wanted.strip().casefold()
    return _Outcome(
        ok,
        Check.VALUE_EQUALS,
        {"check": Check.VALUE_EQUALS.value, "value": actual},
        f"The field holds {actual!r}." if not ok else "The field holds what was typed.",
    )


def _dom_changed(before: PageState, after: PageState) -> _Outcome:
    changed = before.dom_hash != after.dom_hash or before.url != after.url
    return _Outcome(
        changed,
        Check.DOM_CHANGED,
        {
            "check": Check.DOM_CHANGED.value,
            "before": before.dom_hash,
            "after": after.dom_hash,
            "urlChanged": before.url != after.url,
        },
        "The page changed." if changed else "The page is unchanged.",
    )


def _new_tab_opened(before: PageState, after: PageState) -> _Outcome:
    opened = len(after.tabs) > len(before.tabs)
    return _Outcome(
        opened,
        Check.NEW_TAB_OPENED,
        {"check": Check.NEW_TAB_OPENED.value, "before": len(before.tabs), "after": len(after.tabs)},
        "A new tab opened." if opened else "No new tab opened.",
    )


def _scrolled(before: PageState, after: PageState) -> _Outcome:
    moved = before.scroll.y != after.scroll.y
    return _Outcome(
        moved,
        Check.SCROLLED,
        {"check": Check.SCROLLED.value, "before": before.scroll.y, "after": after.scroll.y},
        "The page scrolled." if moved else "The page did not scroll.",
    )


def _extracted(result: ActionResult) -> _Outcome:
    text = (result.extracted or "").strip()
    return _Outcome(
        bool(text),
        Check.EXTRACTED,
        {"check": Check.EXTRACTED.value, "characters": len(text)},
        f"Read {len(text)} characters." if text else "The extract came back empty.",
    )


def _is_stateful(decision: Decision, before: PageState) -> bool:
    """Does the target carry a flag that a click is meant to flip?"""
    if decision.target_index is None:
        return False
    target = before.element_at(decision.target_index)
    if target is None:
        return False
    return target.checked is not None or target.selected is not None or target.expanded is not None


def _element_state(
    decision: Decision, before: PageState, after: PageState, wanted: str
) -> _Outcome:
    """Whether a control's checked, selected or expanded flag moved.

    Flip detection rather than matching a declared value, because the section
    4.5 schema has no outcome type for this and so the planner has no way to
    ask for one. If it ever gains one, this is where the comparison goes.
    """
    original = decision.target_index is not None and before.element_at(decision.target_index)
    target = _same_element(decision, before, after)

    if not original or target is None:
        return _Outcome(
            False,
            Check.ELEMENT_STATE,
            {"check": Check.ELEMENT_STATE.value, "found": None},
            "The control is no longer on the page.",
        )

    was = (original.checked, original.selected, original.expanded)
    now = (target.checked, target.selected, target.expanded)
    moved = was != now

    return _Outcome(
        moved,
        Check.ELEMENT_STATE,
        {
            "check": Check.ELEMENT_STATE.value,
            "before": {"checked": was[0], "selected": was[1], "expanded": was[2]},
            "after": {"checked": now[0], "selected": now[1], "expanded": now[2]},
            "wanted": wanted or None,
        },
        "The control changed state." if moved else "The control did not change state.",
    )


# ----------------------------------------------------------------- lookup


def _as_check(method: VerificationMethod) -> Check:
    return Check(method.value)


def _find(state: PageState, wanted: str) -> ElementInfo | None:
    """An element whose name, text or role matches, loosely."""
    needle = wanted.strip().casefold()
    if not needle:
        return None

    for element in state.elements:
        haystack = f"{element.name} {element.text} {element.role or ''}".casefold()
        if needle in haystack:
            return element
    return None


def _same_element(decision: Decision, before: PageState, after: PageState) -> ElementInfo | None:
    """The element the action targeted, found again in the new page state.

    By CSS path first, because that survives a re-render that reorders the
    page, then by the name the planner used. The index is deliberately not
    used: it is assigned fresh on every perception.
    """
    if decision.target_index is None:
        return None

    original = before.element_at(decision.target_index)
    if original is None:
        return None

    if original.css_path:
        by_path = next(
            (item for item in after.elements if item.css_path == original.css_path), None
        )
        if by_path is not None:
            return by_path

    return next(
        (
            item
            for item in after.elements
            if item.name == original.name and item.tag == original.tag
        ),
        None,
    )
