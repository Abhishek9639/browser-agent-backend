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

import re
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
    ACTIVE_TAB_CHANGED = "active_tab_changed"
    TAB_CLOSED = "tab_closed"
    ELEMENT_STATE = "element_state"
    SCROLLED = "scrolled"
    EXTRACTED = "extracted"
    NOTHING_TO_CHECK = "nothing_to_check"


# Which of the six storable methods each check is recorded as. Spec section 4.7
# writes "URL change/contains" as one item, so both URL checks store as
# url_contains, and the three tab checks store as new_tab_opened for the same
# reason: one storable method per family, with actual["check"] saying which
# member of it ran, so a query for tab outcomes finds all three.
# A control's checked or selected state is its value, so
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
    Check.ACTIVE_TAB_CHANGED: VerificationMethod.NEW_TAB_OPENED,
    Check.TAB_CLOSED: VerificationMethod.NEW_TAB_OPENED,
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
        if check in {Check.ELEMENT_VISIBLE, Check.TEXT_PRESENT} and _is_throwaway(declared.value):
            # "7", "x": a fragment of the goal, not something a page can be
            # held to. Checking it fails a step that worked.
            return self._default_for(decision)
        if decision.action is ActionType.CLICK and _is_dismissal(decision):
            # Closing a popup has one observable effect: the page changed. What
            # the planner expects to see afterwards is a guess about the page
            # underneath, which it has not seen yet.
            return ExpectedOutcome(type=VerificationMethod.DOM_CHANGED, value="")
        return declared

    def _default_for(self, decision: Decision) -> ExpectedOutcome:
        """What counts as success when the planner did not say usefully."""
        match decision.action:
            case ActionType.NAVIGATE:
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
        #
        # The tab actions are here rather than left to the planner's
        # expectation because the tab registry is the ground truth for them
        # and a declared url_contains can pass while the action failed: a
        # new_tab that never opened leaves the old tab active, and if that
        # tab was already on the right host the URL check is satisfied by a
        # page the action never reached.
        if decision.action is ActionType.DONE:
            return _Outcome(True, Check.NOTHING_TO_CHECK, {"action": "done"}, "Nothing to check.")
        if decision.action is ActionType.EXTRACT:
            return _extracted(result)
        if decision.action is ActionType.SCROLL:
            return _scrolled(before, after)
        if decision.action is ActionType.NEW_TAB:
            return _new_tab_opened(before, after)
        if decision.action is ActionType.SWITCH_TAB:
            return _active_tab_changed(before, after)
        if decision.action is ActionType.CLOSE_TAB:
            return _tab_closed(before, after)
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
    """Does any of the page's URLs contain this?

    Frames included. A link inside an iframe navigates that frame and leaves
    the top-level URL untouched, so checking only `url` fails a click that
    did exactly what was asked of it.
    """
    needle = wanted.casefold()
    urls = _urls(after)
    matched = next((url for url in urls if needle in url.casefold()), None)

    return _Outcome(
        matched is not None,
        Check.URL_CONTAINS,
        {
            "check": Check.URL_CONTAINS.value,
            "url": after.url,
            "matched": matched,
            "frames": len(urls),
        },
        f"URL {'contains' if matched else 'does not contain'} {wanted!r}.",
    )


def _url_changed(before: PageState, after: PageState) -> _Outcome:
    """Did the page, or anything framed in it, move?"""
    changed = _urls(before) != _urls(after)
    return _Outcome(
        changed,
        Check.URL_CHANGED,
        {"check": Check.URL_CHANGED.value, "before": before.url, "after": after.url},
        "The URL changed." if changed else "The URL did not change.",
    )


def _urls(state: PageState) -> list[str]:
    """The top-level URL plus every frame's, without duplicates."""
    found = [state.url, *state.frame_urls]
    return list(dict.fromkeys(url for url in found if url))


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
    """Did anything about the page change?

    The hash covers interactive elements only, so a click whose whole effect
    is writing a line into a paragraph would read as nothing having happened.
    The visible text is compared too, because that is the change a person
    would see and most results are rendered as text rather than as controls.
    """
    hash_changed = before.dom_hash != after.dom_hash
    url_changed = _urls(before) != _urls(after)
    text_changed = before.text != after.text
    changed = hash_changed or url_changed or text_changed

    return _Outcome(
        changed,
        Check.DOM_CHANGED,
        {
            "check": Check.DOM_CHANGED.value,
            "before": before.dom_hash,
            "after": after.dom_hash,
            "urlChanged": url_changed,
            "textChanged": text_changed,
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


def _active_tab_changed(before: PageState, after: PageState) -> _Outcome:
    """Is the agent looking at a different tab than it was?"""
    changed = before.active_tab_id != after.active_tab_id
    return _Outcome(
        changed,
        Check.ACTIVE_TAB_CHANGED,
        {
            "check": Check.ACTIVE_TAB_CHANGED.value,
            "before": before.active_tab_id,
            "after": after.active_tab_id,
        },
        f"The active tab is now {after.active_tab_id!r}."
        if changed
        else "The active tab did not change.",
    )


def _tab_closed(before: PageState, after: PageState) -> _Outcome:
    """Counted rather than compared to the active tab.

    Closing a background tab leaves the active one alone, so checking the
    active tab would fail a close that worked perfectly well.
    """
    closed = len(after.tabs) < len(before.tabs)
    return _Outcome(
        closed,
        Check.TAB_CLOSED,
        {"check": Check.TAB_CLOSED.value, "before": len(before.tabs), "after": len(after.tabs)},
        "The tab closed." if closed else "The tab is still open.",
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


_WORDS: Final = re.compile(r"[a-z0-9]+")
# Words that name the kind of control rather than which one it is.
_KIND_WORDS: Final = frozenset(
    {"a", "an", "the", "input", "field", "box", "bar", "button", "link", "text", "element", "icon"}
)


_STOP_WORDS: Final = frozenset({"for", "to", "of", "in", "on", "and", "your", "with", "here"})
# The element tags a kind word implies.
_KIND_TAGS: Final = {
    "input": frozenset({"input", "textarea"}),
    "field": frozenset({"input", "textarea", "select"}),
    "box": frozenset({"input", "textarea", "select"}),
    "button": frozenset({"button"}),
    "link": frozenset({"a"}),
}


_DISMISSAL: Final = re.compile(
    r"\b(close|dismiss|not now|no thanks|maybe later|skip)\b|[×✕]|\b(modal|popup|pop-up|dialog)\b",  # noqa: RUF001
    re.IGNORECASE,
)


def _is_dismissal(decision: Decision) -> bool:
    """Is this click meant to close a popup, going by what the planner called it?"""
    return bool(_DISMISSAL.search(decision.target or ""))


def _is_throwaway(value: str) -> bool:
    """Too short to be a real thing to look for (fewer than three letters or digits)."""
    return (
        len(_WORDS_ALNUM.findall(value)) == 0
        or sum(len(w) for w in _WORDS_ALNUM.findall(value)) < 3
    )


_WORDS_ALNUM: Final = re.compile(r"[A-Za-z0-9]+")


def _find(state: PageState, wanted: str) -> ElementInfo | None:
    """An element whose name, text or role matches, loosely."""
    needle = wanted.strip().casefold()
    if not needle:
        return None

    for element in state.elements:
        haystack = f"{element.name} {element.text} {element.role or ''}".casefold()
        if needle in haystack:
            return element

    # The planner names a control it has not seen yet ("search input field"),
    # and the page calls it something else ("Search here..."). Drop the words
    # that only say what kind of control it is, and require every remaining
    # word. "search results" still does not match a lone "Search" button.
    all_words = _WORDS.findall(needle)
    wanted_words = [w for w in all_words if w not in _KIND_WORDS and w not in _STOP_WORDS]
    # "search input field" must be an input, not the "Search" button next to it.
    kinds = [_KIND_TAGS[w] for w in all_words if w in _KIND_TAGS]
    if not wanted_words:
        # Only a kind was named ("input"): any visible control of that kind counts.
        if not kinds:
            return None
        return next(
            (
                e
                for e in state.elements
                if any(e.tag in tags for tags in kinds) and e.in_viewport and not e.covered
            ),
            None,
        )
    for element in state.elements:
        if kinds and not any(element.tag in tags for tags in kinds):
            continue
        words = set(
            _WORDS.findall(f"{element.name} {element.text} {element.placeholder or ''}".casefold())
        )
        hits = sum(
            1 for word in wanted_words if any(w == word or w.startswith(word) for w in words)
        )
        # A description that names the kind of control ("search input field for
        # internships") only has to share half its meaningful words with the
        # label: the kind already narrows it to inputs. Without a kind, every
        # word must match, so "search results" never matches a lone "Search".
        needed = (len(wanted_words) + 1) // 2 if kinds else len(wanted_words)
        if hits >= max(1, needed):
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
