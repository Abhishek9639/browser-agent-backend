"""Verification accuracy on a labelled set (spec section 9).

Eighteen before/after pairs, each labelled with whether the step really
worked. The verifier's answers are scored against those labels as precision
and recall, because the two errors are not equally bad and lumping them into
one accuracy number hides that.

A false positive says a step worked when it did not: the agent carries on from
a page that is not where it thinks it is, and every step after that is built on
a wrong premise. A false negative says a step failed when it worked: the agent
recovers from a success, burning steps and tokens, and may fail a task that had
already done the thing.

The verifier is deterministic, so both should be perfect. The value is not the
number, it is that a regression names the case that broke.
"""

from dataclasses import dataclass, field
from typing import Any

from app.agent.browser.driver import ActionResult
from app.agent.perception.dom_extractor import BoundingBox, ElementInfo
from app.agent.perception.state import PageState, ScrollState
from app.agent.planner.schema import Decision
from app.agent.verification.verifier import DeterministicVerifier


def element(
    index: int = 0,
    *,
    name: str = "From",
    tag: str = "input",
    text: str = "",
    value: str | None = "",
    input_type: str | None = "text",
    checked: bool | None = None,
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
        checked=checked,
        expanded=expanded,
        css_path=css_path,
        bbox=BoundingBox(x=0, y=0, width=40, height=20),
        in_viewport=True,
    )


def page(
    *elements: ElementInfo,
    url: str = "https://skyfare.test/search",
    dom_hash: str = "h1",
    tabs: list[str] | None = None,
    scroll_y: int = 0,
) -> PageState:
    return PageState(
        url=url,
        title="SkyFare",
        tabs=tabs if tabs is not None else ["t1"],
        active_tab_id="t1",
        elements=list(elements),
        summary_text=" ".join(f"{e.name} {e.text}" for e in elements),
        dom_hash=dom_hash,
        scroll=ScrollState(y=scroll_y, max_y=2000),
    )


def decide(action: str, outcome: str, expected: str = "", **extra: Any) -> Decision:
    """`expected` is what the outcome should match; `value` in extra is what
    the action itself carries, and they are different things."""
    payload: dict[str, Any] = {
        "action": action,
        "reasoning": "because",
        "expectedOutcome": {"type": outcome, "value": expected},
        "confidence": 0.9,
        **extra,
    }
    return Decision.model_validate(payload)


def ran(ok: bool = True, extracted: str | None = None) -> ActionResult:
    return ActionResult(
        ok=ok,
        duration_ms=5,
        url_before="https://skyfare.test/search",
        url_after="https://skyfare.test/search",
        extracted=extracted,
    )


@dataclass(frozen=True)
class Case:
    name: str
    decision: Decision
    before: PageState
    after: PageState
    worked: bool
    result: ActionResult = field(default_factory=ran)


FILLED = element(0, name="From", value="Delhi")
EMPTY = element(0, name="From", value="")

CASES: list[Case] = [
    Case(
        "typing filled the field",
        decide("type", "value_equals", "Delhi", targetIndex=0, value="Delhi"),
        page(EMPTY),
        page(FILLED),
        worked=True,
    ),
    Case(
        "typing left the field empty",
        decide("type", "value_equals", "Delhi", targetIndex=0, value="Delhi"),
        page(EMPTY),
        page(EMPTY),
        worked=False,
    ),
    Case(
        "typing went into the wrong field and the target is unchanged",
        decide("type", "value_equals", "Delhi", targetIndex=0, value="Delhi"),
        page(EMPTY, element(1, name="To", css_path="#to")),
        page(EMPTY, element(1, name="To", value="Delhi", css_path="#to")),
        worked=False,
    ),
    Case(
        "the search button navigated to results",
        decide("click", "dom_changed", targetIndex=0),
        page(element(0, name="Search flights", tag="button", css_path="#go")),
        page(
            element(0, name="Sort by price", tag="a", css_path="#sort"),
            url="https://skyfare.test/results",
            dom_hash="h2",
        ),
        worked=True,
    ),
    Case(
        "the click hit a dead area and nothing happened",
        decide("click", "dom_changed", targetIndex=0),
        page(element(0, name="Search flights", tag="button", css_path="#go")),
        page(element(0, name="Search flights", tag="button", css_path="#go")),
        worked=False,
    ),
    Case(
        "the details link opened a new tab",
        decide("click", "new_tab_opened", targetIndex=0),
        page(element(0, name="Details", tag="a"), tabs=["t1"]),
        page(element(0, name="Details", tag="a"), tabs=["t1", "t2"]),
        worked=True,
    ),
    Case(
        "the details link was expected to open a tab and did not",
        decide("click", "new_tab_opened", targetIndex=0),
        page(element(0, name="Details", tag="a"), tabs=["t1"]),
        page(element(0, name="Details", tag="a"), tabs=["t1"], dom_hash="h9"),
        worked=False,
    ),
    Case(
        "navigation reached the site",
        decide("navigate", "url_contains", "", value="https://skyfare.test/results"),
        page(url="about:blank"),
        page(url="https://skyfare.test/results?from=DEL"),
        worked=True,
    ),
    Case(
        "navigation was redirected somewhere else entirely",
        decide("navigate", "url_contains", "", value="https://skyfare.test/results"),
        page(url="about:blank"),
        page(url="https://login.example.test/sso?next=%2Fresults"),
        worked=False,
    ),
    Case(
        "the non-stop filter was ticked",
        decide("click", "dom_changed", targetIndex=0),
        page(element(0, name="Non-stop only", input_type="checkbox", checked=False)),
        page(element(0, name="Non-stop only", input_type="checkbox", checked=True)),
        worked=True,
    ),
    Case(
        "the disabled filter did not tick",
        decide("click", "dom_changed", targetIndex=0),
        page(element(0, name="Non-stop only", input_type="checkbox", checked=False)),
        page(element(0, name="Non-stop only", input_type="checkbox", checked=False)),
        worked=False,
    ),
    Case(
        "the filter panel expanded",
        decide("click", "dom_changed", targetIndex=0),
        page(element(0, name="More filters", tag="button", expanded=False)),
        page(element(0, name="More filters", tag="button", expanded=True)),
        worked=True,
    ),
    Case(
        "the page scrolled down",
        decide("scroll", "dom_changed"),
        page(scroll_y=0),
        page(scroll_y=800),
        worked=True,
    ),
    Case(
        "scrolling at the bottom of the page moved nothing",
        decide("scroll", "dom_changed"),
        page(scroll_y=2000),
        page(scroll_y=2000),
        worked=False,
    ),
    Case(
        "the extract returned the results",
        decide("extract", "text_present"),
        page(),
        page(),
        worked=True,
        result=ran(extracted="Cheapest fare 4,810"),
    ),
    Case(
        "the extract came back empty",
        decide("extract", "text_present"),
        page(),
        page(),
        worked=False,
        result=ran(extracted="   "),
    ),
    Case(
        "the expected text appeared",
        decide("click", "text_present", "28 flights", targetIndex=0),
        page(element(0, name="Search", tag="button")),
        page(element(0, name="Results", text="28 flights found"), dom_hash="h3"),
        worked=True,
    ),
    Case(
        "the expected text never appeared",
        decide("click", "text_present", "28 flights", targetIndex=0),
        page(element(0, name="Search", tag="button")),
        page(element(0, name="Sorry", text="No flights match"), dom_hash="h3"),
        worked=False,
    ),
]


async def score() -> tuple[dict[str, int], list[str]]:
    verifier = DeterministicVerifier()
    counts = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
    wrong: list[str] = []

    for case in CASES:
        verdict = await verifier.check(case.decision, case.before, case.after, case.result)
        if case.worked and verdict.ok:
            counts["tp"] += 1
        elif case.worked and not verdict.ok:
            counts["fn"] += 1
            wrong.append(f"false negative: {case.name} ({verdict.detail})")
        elif not case.worked and verdict.ok:
            counts["fp"] += 1
            wrong.append(f"FALSE POSITIVE: {case.name} ({verdict.detail})")
        else:
            counts["tn"] += 1

    return counts, wrong


async def test_the_labelled_set_covers_both_answers() -> None:
    worked = [case for case in CASES if case.worked]

    assert len(CASES) >= 15
    # A set of only successes would score perfectly on a verifier that says
    # yes to everything, which is exactly the verifier this replaced.
    assert len(worked) >= 6
    assert len(CASES) - len(worked) >= 6
    assert len({case.name for case in CASES}) == len(CASES)


async def test_verification_precision_and_recall(capsys: Any) -> None:
    counts, wrong = await score()

    precision = counts["tp"] / max(1, counts["tp"] + counts["fp"])
    recall = counts["tp"] / max(1, counts["tp"] + counts["fn"])

    with capsys.disabled():
        print(  # noqa: T201
            f"\nverification accuracy over {len(CASES)} cases: "
            f"precision {precision:.2f}, recall {recall:.2f} "
            f"(tp={counts['tp']} fp={counts['fp']} tn={counts['tn']} fn={counts['fn']})"
        )
        for line in wrong:
            print(f"  {line}")  # noqa: T201

    assert not wrong, "\n".join(wrong)
    assert precision == 1.0
    assert recall == 1.0
