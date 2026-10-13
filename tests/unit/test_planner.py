"""B14: the planner, its guards, and its one repair turn."""

from typing import Any

import pytest

from app.agent.llm.mock import MockLLM
from app.agent.perception.dom_extractor import BoundingBox, ElementInfo
from app.agent.perception.state import PageState
from app.agent.planner.planner import (
    HISTORY_WINDOW,
    Planner,
    PlannerOutputError,
    StepSummary,
    compress_history,
)
from app.agent.planner.registry import PromptRegistry
from app.agent.planner.schema import Decision

GOAL = "find the cheapest flight from Delhi to Bengaluru on 20 Oct"


def element(index: int, tag: str = "button", name: str = "Search flights") -> ElementInfo:
    return ElementInfo(
        index=index,
        tag=tag,
        name=name,
        bbox=BoundingBox(x=0, y=0, width=10, height=10),
        in_viewport=True,
    )


def page(*indices: int) -> PageState:
    elements = [element(index) for index in indices]
    lines = "\n".join(f'[{index}] button "Search flights"' for index in indices)
    return PageState(
        url="http://sky.test/search",
        title="SkyFare",
        elements=elements,
        summary_text=f"URL: http://sky.test/search\nTitle: SkyFare\n\n{lines}",
    )


def valid(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": "click",
        "targetIndex": 0,
        "target": "Search flights",
        "value": None,
        "tabId": None,
        "reasoning": "the form is filled",
        "expectedOutcome": {"type": "dom_changed", "value": ""},
        "riskLevel": "LOW",
        "confidence": 0.9,
        "requiresApproval": False,
        "needsVision": False,
        "finalAnswer": None,
    }
    return payload | overrides


# ------------------------------------------------------------------ happy


async def test_a_valid_decision_comes_straight_back() -> None:
    """TEST-004: a valid summarised page state yields a valid next action."""
    planner = Planner(MockLLM(script=[valid()]))

    result = await planner.decide(GOAL, page(0))

    assert result.decision.target_index == 0
    assert result.attempts == 1
    assert not result.needed_repair
    assert result.prompt_version == "v1"


async def test_the_prompt_version_is_recorded() -> None:
    """Spec section 9 wants it on every step; a prompt change moves behaviour."""
    planner = Planner(MockLLM(script=[valid()]), PromptRegistry("v1"))

    assert (await planner.decide(GOAL, page(0))).prompt_version == "v1"


async def test_usage_and_latency_come_through() -> None:
    result = await Planner(MockLLM(script=[valid()])).decide(GOAL, page(0))

    assert result.llm_response.usage.input_tokens > 0
    assert result.llm_response.latency_ms >= 0


# ------------------------------------------------------------ groundedness


async def test_an_index_not_on_the_page_is_rejected() -> None:
    """The target-existence guard from spec section 4.11.

    This is what makes "never invent an element" enforceable rather than
    merely requested.
    """
    llm = MockLLM(script=[valid(targetIndex=99), valid(targetIndex=1)])
    planner = Planner(llm)

    result = await planner.decide(GOAL, page(0, 1, 2))

    assert result.decision.target_index == 1
    assert result.attempts == 2
    assert result.needed_repair


async def test_the_repair_turn_names_the_available_indices() -> None:
    llm = MockLLM(script=[valid(targetIndex=99), valid(targetIndex=0)])

    await Planner(llm).decide(GOAL, page(0, 1))

    repair = llm.calls[1].messages[-1].text
    assert "99 is not in the page state" in repair
    assert "0, 1" in repair


async def test_a_second_bad_index_fails_the_step() -> None:
    llm = MockLLM(script=[valid(targetIndex=99), valid(targetIndex=98)])

    with pytest.raises(PlannerOutputError, match="not in the page state"):
        await Planner(llm).decide(GOAL, page(0))

    assert llm.call_count == 2, "exactly one repair turn, per spec section 4.5"


async def test_an_action_with_no_index_skips_the_guard() -> None:
    planner = Planner(MockLLM(script=[valid(action="scroll", targetIndex=None)]))

    result = await planner.decide(GOAL, page(0))

    assert result.decision.action.value == "scroll"


# ------------------------------------------------------------------ repair


async def test_an_invalid_decision_buys_one_repair() -> None:
    llm = MockLLM(script=[valid(action="done", targetIndex=None), valid()])

    result = await Planner(llm).decide(GOAL, page(0))

    assert result.attempts == 2
    assert result.decision.action.value == "click"


async def test_the_repair_turn_carries_the_validation_error() -> None:
    llm = MockLLM(script=[valid(action="done", targetIndex=None), valid()])

    await Planner(llm).decide(GOAL, page(0))

    assert "finalAnswer" in llm.calls[1].messages[-1].text


async def test_malformed_json_buys_one_repair_then_fails() -> None:
    """TEST-006: malformed JSON, one repair retry, then the step fails."""
    llm = MockLLM(malformed_times=99)

    with pytest.raises(PlannerOutputError, match="usable JSON"):
        await Planner(llm).decide(GOAL, page(0))

    assert llm.call_count == 2


async def test_malformed_then_valid_recovers() -> None:
    llm = MockLLM(malformed_times=1, script=[valid()])

    result = await Planner(llm).decide(GOAL, page(0))

    assert result.attempts == 2


async def test_a_transport_failure_is_not_the_planner_s_problem() -> None:
    """Retries belong to RetryingLLM; the planner does not double up."""
    from app.agent.llm.base import LLMUnavailable

    llm = MockLLM(fail_times=99, failure=LLMUnavailable)

    with pytest.raises(LLMUnavailable):
        await Planner(llm).decide(GOAL, page(0))

    assert llm.call_count == 1


# ------------------------------------------------------------------ prompt


async def test_the_page_state_is_fenced_as_untrusted() -> None:
    llm = MockLLM(script=[valid()])

    await Planner(llm).decide(GOAL, page(0))

    text = llm.calls[0].messages[0].text
    assert "<page_state>" in text
    assert "</page_state>" in text
    assert "untrusted" in text.lower()


async def test_the_goal_and_history_precede_the_page_state() -> None:
    """Stable content first, so prompt caching can work when it is added."""
    llm = MockLLM(script=[valid()])

    await Planner(llm).decide(GOAL, page(0), [StepSummary(1, "navigate", verified=True)])

    text = llm.calls[0].messages[0].text
    assert text.index("GOAL") < text.index("HISTORY") < text.index("PAGE STATE")


async def test_a_cached_hint_is_labelled_as_not_the_current_page() -> None:
    llm = MockLLM(script=[valid()])

    await Planner(llm).decide(GOAL, page(0), cached_hint="click [2] then type Delhi")

    text = llm.calls[0].messages[0].text
    assert "RETRIEVED HINT" in text
    assert "verify against the page state" in text


async def test_no_hint_means_no_hint_section() -> None:
    llm = MockLLM(script=[valid()])

    await Planner(llm).decide(GOAL, page(0))

    assert "RETRIEVED HINT" not in llm.calls[0].messages[0].text


async def test_the_schema_is_sent_to_the_provider() -> None:
    llm = MockLLM(script=[valid()])

    await Planner(llm).decide(GOAL, page(0))

    schema = llm.calls[0].json_schema
    assert schema["properties"]["action"]["enum"]
    assert schema["additionalProperties"] is False


async def test_the_system_prompt_is_the_registry_s() -> None:
    llm = MockLLM(script=[valid()])

    await Planner(llm).decide(GOAL, page(0))

    assert llm.calls[0].system == PromptRegistry("v1").system_prompt


# ------------------------------------------------------------------ history


def test_no_history_says_so() -> None:
    assert compress_history([]) == "No actions taken yet."


def test_recent_steps_are_rendered_in_full() -> None:
    steps = [StepSummary(1, "navigate", "sky.test", True), StepSummary(2, "click", "Search", False)]

    rendered = compress_history(steps)

    assert "1. navigate sky.test -> verified" in rendered
    assert "2. click Search -> failed" in rendered


def test_a_pending_step_says_pending() -> None:
    assert "pending" in compress_history([StepSummary(1, "click", "x", None)])


def test_older_steps_collapse_to_one_line() -> None:
    """A 25 step task would otherwise spend its prompt restating itself."""
    steps = [StepSummary(n, "click", f"thing {n}", verified=n % 2 == 0) for n in range(1, 21)]

    lines = compress_history(steps).splitlines()

    assert len(lines) == HISTORY_WINDOW + 1
    assert lines[0].startswith("[12 earlier steps,")
    assert "6 verified]" in lines[0]


def test_exactly_the_window_is_not_summarised() -> None:
    steps = [StepSummary(n, "click", verified=True) for n in range(1, HISTORY_WINDOW + 1)]

    lines = compress_history(steps).splitlines()

    assert len(lines) == HISTORY_WINDOW
    assert not lines[0].startswith("[")


def test_the_window_is_configurable() -> None:
    steps = [StepSummary(n, "click", verified=True) for n in range(1, 11)]

    assert len(compress_history(steps, window=3).splitlines()) == 4


# ------------------------------------------------------------------ registry


def test_the_prompt_forbids_following_page_instructions() -> None:
    """Spec section 4.11 requires this to be in the system prompt."""
    prompt = PromptRegistry("v1").system_prompt.lower()

    assert "untrusted" in prompt
    assert "<page_state>" in prompt
    assert "never" in prompt


@pytest.mark.parametrize(
    "requirement",
    [
        "never invent an index",
        "requiresapproval",
        "needsvision",
        "expectedoutcome",
        "one action per turn",
        "assumption",
        "irreversible",
        "observable",
    ],
)
def test_the_prompt_covers_each_required_instruction(requirement: str) -> None:
    assert requirement in PromptRegistry("v1").system_prompt.lower()


def test_an_unknown_prompt_version_fails_at_construction() -> None:
    from app.agent.planner.registry import PromptNotFoundError

    with pytest.raises(PromptNotFoundError, match="v99"):
        PromptRegistry("v99")


def test_the_prompt_is_not_empty() -> None:
    assert len(PromptRegistry("v1").system_prompt) > 1000


# ------------------------------------------------------------------ eval set


def test_the_eval_dataset_is_loadable_and_complete() -> None:
    """A case naming a fixture that does not exist would silently never run."""
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[2]
    cases = yaml.safe_load(
        (root / "tests" / "eval" / "planner_cases.yaml").read_text(encoding="utf-8")
    )["cases"]

    assert len(cases) >= 20
    assert len({case["id"] for case in cases}) == len(cases), "duplicate case id"

    for case in cases:
        assert (root / "tests" / "fixtures" / "html" / case["page"]).is_file(), case["id"]
        assert case["acceptable"], case["id"]
        for entry in case["acceptable"]:
            assert "action" in entry, case["id"]


def test_every_acceptable_action_is_on_the_allow_list() -> None:
    from pathlib import Path

    import yaml

    from app.domain.enums import ActionType

    root = Path(__file__).resolve().parents[2]
    cases = yaml.safe_load(
        (root / "tests" / "eval" / "planner_cases.yaml").read_text(encoding="utf-8")
    )["cases"]
    allowed = {member.value for member in ActionType}

    for case in cases:
        for entry in case["acceptable"]:
            assert entry["action"] in allowed, f"{case['id']}: {entry['action']}"


# ------------------------------------------------------------------ covered inputs


def _field(index: int, *, covered: bool, name: str = "") -> ElementInfo:
    return ElementInfo(
        index=index,
        tag="input",
        name=name,
        input_type="text",
        bbox=BoundingBox(x=10, y=10 * index, width=100, height=20),
        in_viewport=True,
        covered=covered,
    )


def _type_decision(index: int) -> Decision:
    return Decision.model_validate(
        {
            "action": "type",
            "targetIndex": index,
            "target": "search",
            "value": "Full Stack",
            "expectedOutcome": {"type": "value_equals", "value": "Full Stack"},
        }
    )


def _state_with(*elements: ElementInfo) -> PageState:
    return PageState(url="https://example.test/", elements=list(elements))


def test_typing_into_a_covered_box_is_refused_when_a_visible_one_exists() -> None:
    from app.agent.planner.planner import _covered_input_problem

    state = _state_with(_field(11, covered=True), _field(24, covered=False, name="Search"))

    problem = _covered_input_problem(_type_decision(11), state)

    assert problem is not None
    assert "24" in problem


def test_a_covered_box_is_allowed_when_it_is_the_only_input() -> None:
    from app.agent.planner.planner import _covered_input_problem

    state = _state_with(_field(11, covered=True))

    assert _covered_input_problem(_type_decision(11), state) is None


def test_a_visible_target_is_never_refused() -> None:
    from app.agent.planner.planner import _covered_input_problem

    state = _state_with(_field(11, covered=True), _field(24, covered=False, name="Search"))

    assert _covered_input_problem(_type_decision(24), state) is None


def test_an_unnamed_input_is_refused_when_a_named_one_is_below_the_fold() -> None:
    from app.agent.planner.planner import _covered_input_problem

    unnamed = _field(8, covered=False)
    named = ElementInfo(
        index=21,
        tag="input",
        name="e.g. Design, Mumbai",
        input_type="text",
        bbox=BoundingBox(x=0, y=900, width=100, height=20),
        in_viewport=False,
    )

    problem = _covered_input_problem(_type_decision(8), _state_with(unnamed, named))

    assert problem is not None
    assert "21" in problem
    assert "scroll" in problem
