"""Deterministic verification (spec section 4.7, ADR-004).

Built from hand-made page states rather than a browser: a check is a
comparison between two snapshots, and constructing the two directly is both
faster and far clearer about what is being compared. The browser end of it,
waiting for a page to stop moving, is tested separately against real pages.
"""

from pathlib import Path
from typing import Any

import pytest

from app.agent.browser.driver import ActionResult
from app.agent.perception.dom_extractor import MASK, BoundingBox, ElementInfo
from app.agent.perception.state import PageState, ScrollState
from app.agent.planner.schema import Decision
from app.agent.verification.verifier import STORED_AS, Check, DeterministicVerifier
from app.domain.enums import VerificationMethod

VERIFIER = DeterministicVerifier()


def element(
    index: int = 0,
    *,
    name: str = "From",
    tag: str = "input",
    text: str = "",
    value: str | None = "",
    input_type: str | None = "text",
    role: str | None = None,
    checked: bool | None = None,
    selected: bool | None = None,
    expanded: bool | None = None,
    css_path: str = "#from",
) -> ElementInfo:
    return ElementInfo(
        index=index,
        tag=tag,
        name=name,
        text=text,
        value=value,
        input_type=input_type,
        role=role,
        checked=checked,
        selected=selected,
        expanded=expanded,
        css_path=css_path,
        bbox=BoundingBox(x=0, y=0, width=50, height=20),
        in_viewport=True,
    )


def page(
    *elements: ElementInfo,
    url: str = "https://skyfare.test/search",
    summary: str = "",
    dom_hash: str = "aaaa",
    tabs: list[str] | None = None,
    active: str = "t1",
    scroll_y: int = 0,
) -> PageState:
    return PageState(
        url=url,
        title="SkyFare",
        tabs=tabs if tabs is not None else ["t1"],
        active_tab_id=active,
        elements=list(elements),
        summary_text=summary or " ".join(e.name for e in elements),
        dom_hash=dom_hash,
        scroll=ScrollState(y=scroll_y, max_y=2000),
    )


def decision(
    action: str = "click",
    *,
    outcome: str = "dom_changed",
    value: str = "",
    target_index: int | None = 0,
    decision_value: str | None = None,
    tab_id: str | None = None,
) -> Decision:
    payload: dict[str, Any] = {
        "action": action,
        "reasoning": "because",
        "expectedOutcome": {"type": outcome, "value": value},
        "confidence": 0.9,
    }
    if tab_id is not None:
        payload["tabId"] = tab_id
    if target_index is not None:
        payload["targetIndex"] = target_index
    if decision_value is not None:
        payload["value"] = decision_value
    if action == "done":
        payload["finalAnswer"] = "done"
    return Decision.model_validate(payload)


def ran(*, ok: bool = True, extracted: str | None = None) -> ActionResult:
    return ActionResult(
        ok=ok,
        duration_ms=10,
        url_before="https://skyfare.test/search",
        url_after="https://skyfare.test/search",
        extracted=extracted,
    )


async def verify(decided: Decision, before: PageState, after: PageState, **kwargs: Any) -> Any:
    return await VERIFIER.check(decided, before, after, ran(**kwargs))


# --------------------------------------------------------- one check each


async def test_url_contains_passes_when_it_does() -> None:
    verdict = await verify(
        decision(outcome="url_contains", value="/results"),
        page(),
        page(url="https://skyfare.test/results?from=DEL"),
    )

    assert verdict.ok is True
    assert verdict.method is VerificationMethod.URL_CONTAINS
    assert verdict.actual["check"] == Check.URL_CONTAINS.value


async def test_url_contains_fails_when_it_does_not() -> None:
    verdict = await verify(
        decision(outcome="url_contains", value="/results"),
        page(),
        page(url="https://skyfare.test/search"),
    )

    assert verdict.ok is False
    assert "/results" in verdict.detail


async def test_url_changed_passes_when_the_page_moved() -> None:
    verdict = await verify(
        decision(action="go_back", outcome="url_contains", value="", target_index=None),
        page(url="https://skyfare.test/results"),
        page(url="https://skyfare.test/search"),
    )

    assert verdict.ok is True
    assert verdict.actual["check"] == Check.URL_CHANGED.value


async def test_url_changed_fails_when_it_did_not() -> None:
    verdict = await verify(
        decision(action="go_back", outcome="url_contains", value="", target_index=None),
        page(url="https://skyfare.test/search"),
        page(url="https://skyfare.test/search"),
    )

    assert verdict.ok is False


async def test_element_visible_finds_by_name_text_or_role() -> None:
    after = page(
        element(0, name="Search flights", tag="button"),
        element(1, name="", text="28 flights found", tag="div"),
        element(2, name="", tag="div", role="alert"),
    )

    for wanted in ("Search flights", "28 flights", "alert"):
        verdict = await verify(decision(outcome="element_visible", value=wanted), page(), after)
        assert verdict.ok is True, wanted


async def test_element_visible_fails_when_it_is_absent() -> None:
    verdict = await verify(
        decision(outcome="element_visible", value="Book now"),
        page(),
        page(element(0, name="Search flights")),
    )

    assert verdict.ok is False
    assert verdict.actual["found"] is None


async def test_text_present_passes_and_fails() -> None:
    after = page(element(0, name="Result", text="Cheapest fare 4,810"))

    assert (await verify(decision(outcome="text_present", value="4,810"), page(), after)).ok
    assert not (await verify(decision(outcome="text_present", value="9,999"), page(), after)).ok


async def test_value_equals_rereads_the_field() -> None:
    before = page(element(0, name="From", value=""))
    after = page(element(0, name="From", value="Delhi"))

    verdict = await verify(
        decision(action="type", outcome="value_equals", value="Delhi", decision_value="Delhi"),
        before,
        after,
    )

    assert verdict.ok is True
    assert verdict.actual["value"] == "Delhi"


async def test_value_equals_fails_on_the_wrong_value() -> None:
    before = page(element(0, name="From", value=""))
    after = page(element(0, name="From", value="Delh"))

    verdict = await verify(
        decision(action="type", outcome="value_equals", value="Delhi", decision_value="Delhi"),
        before,
        after,
    )

    assert verdict.ok is False
    assert "'Delh'" in verdict.detail


async def test_value_equals_finds_the_field_again_after_a_reorder() -> None:
    before = page(element(0, name="From", css_path="#from"))
    # Re-perceiving assigns indices fresh, so the field the action targeted is
    # now at a different index. Matching on the index would check the wrong one.
    after = page(
        element(0, name="Promo code", css_path="#promo"),
        element(1, name="From", value="Delhi", css_path="#from"),
    )

    verdict = await verify(
        decision(action="type", outcome="value_equals", value="Delhi", decision_value="Delhi"),
        before,
        after,
    )

    assert verdict.ok is True


async def test_a_masked_field_cannot_be_compared_and_is_taken_on_trust() -> None:
    before = page(element(0, name="Password", value="", input_type="password"))
    after = page(element(0, name="Password", value=MASK, input_type="password"))

    verdict = await verify(
        decision(action="type", outcome="value_equals", value="hunter2", decision_value="hunter2"),
        before,
        after,
    )

    # The collector masks secrets at source, so the real value is not here to
    # compare and never will be. Failing every password field instead would
    # make the agent unable to log in to anything.
    assert verdict.ok is True
    assert verdict.actual["masked"] is True


async def test_value_equals_fails_when_the_field_has_gone() -> None:
    verdict = await verify(
        decision(action="type", outcome="value_equals", value="Delhi", decision_value="Delhi"),
        page(element(0, name="From")),
        page(),
    )

    assert verdict.ok is False
    assert verdict.actual["found"] is None


async def test_dom_changed_compares_the_hash() -> None:
    assert (await verify(decision(), page(dom_hash="aaaa"), page(dom_hash="bbbb"))).ok is True
    assert (await verify(decision(), page(dom_hash="aaaa"), page(dom_hash="aaaa"))).ok is False


async def test_dom_changed_also_accepts_a_navigation() -> None:
    # A click that navigates has done something, even to a page whose element
    # list happens to hash the same.
    verdict = await verify(
        decision(),
        page(url="https://skyfare.test/a", dom_hash="aaaa"),
        page(url="https://skyfare.test/b", dom_hash="aaaa"),
    )

    assert verdict.ok is True
    assert verdict.actual["urlChanged"] is True


async def test_new_tab_opened_counts_tabs() -> None:
    assert (
        await verify(decision(outcome="new_tab_opened"), page(tabs=["t1"]), page(tabs=["t1", "t2"]))
    ).ok is True
    assert (
        await verify(decision(outcome="new_tab_opened"), page(tabs=["t1"]), page(tabs=["t1"]))
    ).ok is False


async def test_a_new_tab_is_verified_by_the_registry_not_by_the_url() -> None:
    """The planner's own expectation does not get a say here.

    A ``new_tab`` that never opened leaves the old tab active, and if that tab
    was already on the right host a declared ``url_contains`` passes on a page
    the action never reached. Counting tabs cannot be fooled that way.
    """
    verdict = await verify(
        decision("new_tab", outcome="url_contains", value="skyfare.test"),
        page(tabs=["t1"]),
        page(tabs=["t1"]),
    )

    assert verdict.ok is False
    assert verdict.actual["check"] == Check.NEW_TAB_OPENED.value


async def test_switching_tabs_is_verified_by_the_active_tab() -> None:
    passed = await verify(
        decision("switch_tab", tab_id="t2"),
        page(tabs=["t1", "t2"], active="t1"),
        page(tabs=["t1", "t2"], active="t2"),
    )
    failed = await verify(
        decision("switch_tab", tab_id="t2"),
        page(tabs=["t1", "t2"], active="t1"),
        page(tabs=["t1", "t2"], active="t1"),
    )

    assert passed.ok is True
    assert passed.actual["check"] == Check.ACTIVE_TAB_CHANGED.value
    assert passed.actual["after"] == "t2"
    assert failed.ok is False


async def test_closing_a_background_tab_still_verifies() -> None:
    """Counted rather than compared to the active tab.

    Closing a tab the agent was not looking at leaves the active tab alone, so
    an "active tab changed" check would fail a close that worked.
    """
    verdict = await verify(
        decision("close_tab", tab_id="t2"),
        page(tabs=["t1", "t2"], active="t1"),
        page(tabs=["t1"], active="t1"),
    )

    assert verdict.ok is True
    assert verdict.actual["check"] == Check.TAB_CLOSED.value


async def test_the_three_tab_checks_store_as_one_method() -> None:
    """One storable method per family, with the detail in ``actual``.

    Spec section 4.5 gives six outcome types and the column holds one of them.
    Widening the enum to carry a distinction that belongs in JSON would mean a
    migration for every check added afterwards.
    """
    for check in (Check.NEW_TAB_OPENED, Check.ACTIVE_TAB_CHANGED, Check.TAB_CLOSED):
        assert STORED_AS[check] is VerificationMethod.NEW_TAB_OPENED


async def test_element_state_notices_a_checkbox_flipping() -> None:
    before = page(element(0, name="Non-stop only", input_type="checkbox", checked=False))
    after = page(element(0, name="Non-stop only", input_type="checkbox", checked=True))

    verdict = await verify(decision(), before, after)

    assert verdict.ok is True
    assert verdict.actual["check"] == Check.ELEMENT_STATE.value


async def test_element_state_fails_when_the_control_did_not_move() -> None:
    same = page(element(0, name="Non-stop only", input_type="checkbox", checked=False))

    verdict = await verify(decision(), same, same)

    assert verdict.ok is False


async def test_a_disclosure_toggle_is_verified_by_its_flag() -> None:
    # `expanded` is not part of the DOM hash, so dom_changed would call this a
    # failure even though the click did exactly what it was meant to.
    before = page(element(0, name="More filters", tag="button", expanded=False))
    after = page(element(0, name="More filters", tag="button", expanded=True))

    verdict = await verify(decision(), before, after)

    assert verdict.ok is True
    assert verdict.actual["after"]["expanded"] is True


# ------------------------------------------------------- defaults by action


async def test_navigate_defaults_to_the_host_when_nothing_useful_was_asked() -> None:
    verdict = await verify(
        decision(
            action="navigate",
            outcome="url_contains",
            value="",
            target_index=None,
            decision_value="https://skyfare.test/results",
        ),
        page(url="about:blank"),
        page(url="https://skyfare.test/results"),
    )

    assert verdict.ok is True
    assert verdict.expected["value"] == "skyfare.test"


async def test_type_defaults_to_the_value_that_was_typed() -> None:
    verdict = await verify(
        decision(action="type", outcome="text_present", value="", decision_value="Delhi"),
        page(element(0, name="From", value="")),
        page(element(0, name="From", value="Delhi")),
    )

    assert verdict.ok is True
    assert verdict.expected["check"] == Check.VALUE_EQUALS.value


async def test_select_defaults_to_the_value_that_was_chosen() -> None:
    verdict = await verify(
        decision(action="select", outcome="element_visible", value="", decision_value="Economy"),
        page(element(0, name="Cabin", tag="select", value="")),
        page(element(0, name="Cabin", tag="select", value="Economy")),
    )

    assert verdict.ok is True
    assert verdict.expected["check"] == Check.VALUE_EQUALS.value


async def test_click_defaults_to_something_having_changed() -> None:
    verdict = await verify(
        decision(action="click", outcome="text_present", value=""),
        page(dom_hash="aaaa"),
        page(dom_hash="bbbb"),
    )

    assert verdict.ok is True
    assert verdict.expected["check"] == Check.DOM_CHANGED.value


async def test_scroll_is_verified_by_the_scroll_position() -> None:
    moved = await verify(
        decision(action="scroll", target_index=None), page(scroll_y=0), page(scroll_y=600)
    )
    stuck = await verify(
        decision(action="scroll", target_index=None), page(scroll_y=0), page(scroll_y=0)
    )

    assert moved.ok is True
    assert moved.actual["after"] == 600
    # A scroll at the bottom of the page moves nothing, and the planner needs
    # to know that rather than being told it worked.
    assert stuck.ok is False


async def test_extract_is_verified_by_getting_something_back() -> None:
    full = await verify(
        decision(action="extract", target_index=None), page(), page(), extracted="4,810"
    )
    empty = await verify(
        decision(action="extract", target_index=None), page(), page(), extracted="   "
    )

    assert full.ok is True
    assert full.actual["characters"] == 5
    assert empty.ok is False


async def test_done_always_passes() -> None:
    verdict = await verify(
        decision(action="done", target_index=None), page(dom_hash="aaaa"), page(dom_hash="aaaa")
    )

    assert verdict.ok is True
    assert verdict.actual["action"] == "done"


# ---------------------------------------------------------------- storage


@pytest.mark.parametrize("check", list(Check))
def test_every_check_maps_to_a_storable_method(check: Check) -> None:
    # verifications.verification_method is a Postgres enum of the six section
    # 4.5 outcome types. A check that has nowhere to be stored cannot be run.
    assert STORED_AS[check] in set(VerificationMethod)


async def test_the_verdict_records_what_was_compared() -> None:
    verdict = await verify(
        decision(outcome="url_contains", value="/results"),
        page(),
        page(url="https://skyfare.test/results"),
    )

    assert verdict.expected["type"] == "url_contains"
    assert verdict.expected["value"] == "/results"
    assert verdict.actual["url"] == "https://skyfare.test/results"
    assert verdict.detail


def test_the_verifier_never_reaches_for_a_model() -> None:
    """ADR-004, enforced rather than asserted in prose.

    The model proposes actions. A model that is wrong about what a click did
    is exactly the model that will say confidently that it worked, so it must
    not also be the thing that decides whether it did.
    """
    import app.agent.verification.verifier as module

    text = Path(module.__file__ or "").read_text(encoding="utf-8")

    assert "llm" not in text.lower().replace("llm_", "")
    assert "app.agent.llm" not in text


# ------------------------------------------------------------------ loose names


def _named(index: int, name: str, placeholder: str | None = None) -> ElementInfo:
    return ElementInfo(
        index=index,
        tag="input",
        name=name,
        placeholder=placeholder,
        bbox=BoundingBox(x=0, y=0, width=10, height=10),
        in_viewport=True,
    )


def test_a_planner_description_finds_the_control_the_page_calls_something_else() -> None:
    from app.agent.verification.verifier import _find

    state = PageState(url="https://x.test/", elements=[_named(3, "Search here...")])

    assert _find(state, "search input field") is not None


def test_every_meaningful_word_must_match() -> None:
    from app.agent.verification.verifier import _find

    state = PageState(url="https://x.test/", elements=[_named(3, "Search")])

    assert _find(state, "search results") is None
    assert _find(state, "input field") is None


def test_an_input_description_does_not_match_a_button() -> None:
    from app.agent.verification.verifier import _find

    button = ElementInfo(
        index=5,
        tag="button",
        name="Search",
        bbox=BoundingBox(x=0, y=0, width=10, height=10),
        in_viewport=True,
    )
    state = PageState(url="https://x.test/", elements=[button])

    assert _find(state, "search input field") is None
