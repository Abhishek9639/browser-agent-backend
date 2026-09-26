"""B12: the mock provider (spec section 11).

"A mock LLM provider makes the agent loop testable without API quota." These
tests cover both modes, the call recording, and the failure injection the retry
and repair paths are tested with.
"""

import pytest

from app.agent.llm.base import LLMBadOutput, LLMMessage, LLMTimeout, MessageRole
from app.agent.llm.mock import _PRICE, MockLLM, parse_summary

GOAL = "find the cheapest flight from Delhi to Bengaluru on 20 Oct"

SEARCH_PAGE = """URL: http://sky.test/search
Title: SkyFare search
Tabs: t1 (active)
Scroll: 0 of 0

[0] input[type=text] "From" placeholder="Departure city"
[1] input[type=text] "To" placeholder="Arrival city"
[2] input[type=date] "Date"
[3] button "Search flights"
"""

RESULTS_PAGE = """URL: http://sky.test/results
Title: Results
Tabs: t1 (active)
Scroll: 0 of 0

[0] a "Cheapest" href=/results?sort=price
[1] a "Fastest" href=/results?sort=duration
[2] button "Book AI-812"
"""

SORTED_PAGE = RESULTS_PAGE.replace(
    "URL: http://sky.test/results", "URL: http://sky.test/results?sort=price"
)


def user(text: str) -> LLMMessage:
    return LLMMessage(role=MessageRole.USER, text=text)


async def decide(llm: MockLLM, *texts: str) -> dict[str, object]:
    response = await llm.generate_json("system", [user(text) for text in texts], {})
    return response.parsed


# ------------------------------------------------------------------ parsing


def test_summary_lines_are_parsed_back() -> None:
    """The mock reads exactly what a real model would be shown."""
    lines = parse_summary(SEARCH_PAGE)

    assert [line.index for line in lines] == [0, 1, 2, 3]
    assert lines[0].tag == "input"
    assert lines[0].input_type == "text"
    assert lines[0].name == "From"
    assert lines[0].placeholder == "Departure city"
    assert lines[3].tag == "button"


def test_value_and_placeholder_are_not_confused_with_the_name() -> None:
    lines = parse_summary('[7] input[type=text] "From" placeholder="City" value="Delhi"')

    assert (lines[0].name, lines[0].placeholder, lines[0].value) == ("From", "City", "Delhi")


def test_href_is_parsed() -> None:
    assert parse_summary('[2] a "Cheapest" href=/results?sort=price')[0].href == (
        "/results?sort=price"
    )


def test_header_lines_are_ignored() -> None:
    assert parse_summary('URL: http://x.test\nTitle: T\n\n[0] button "Go"')[0].index == 0


# ------------------------------------------------------------------ scripted


async def test_scripted_decisions_come_back_in_order() -> None:
    llm = MockLLM(script=[{"action": "click", "n": 1}, {"action": "done", "n": 2}])

    assert (await decide(llm, GOAL))["n"] == 1
    assert (await decide(llm, GOAL))["n"] == 2


async def test_the_script_can_be_extended() -> None:
    llm = MockLLM()
    llm.push({"action": "wait"}, {"action": "done"})

    assert llm.script_remaining == 2
    assert (await decide(llm, GOAL))["action"] == "wait"


async def test_rules_take_over_when_the_script_runs_out() -> None:
    llm = MockLLM(script=[{"action": "wait"}])
    await decide(llm, GOAL)

    decision = await decide(llm, GOAL, SEARCH_PAGE)

    assert decision["action"] in {"type", "click", "extract", "done"}


# ------------------------------------------------------------------ recording


async def test_every_call_is_recorded() -> None:
    llm = MockLLM(script=[{"action": "done"}])

    await llm.generate_json("you are an agent", [user(GOAL)], {"type": "object"})

    assert llm.call_count == 1
    call = llm.calls[0]
    assert call.system == "you are an agent"
    assert call.last_user_text == GOAL
    assert call.json_schema == {"type": "object"}


async def test_images_are_counted_from_both_places() -> None:
    llm = MockLLM(script=[{"action": "done"}])
    message = LLMMessage(role=MessageRole.USER, text="look", images=[b"a", b"b"])

    await llm.generate_json("s", [message], {}, images=[b"c"])

    assert llm.calls[0].image_count == 3


async def test_reset_clears_state() -> None:
    llm = MockLLM(script=[{"action": "done"}])
    await decide(llm, GOAL)

    llm.reset()

    assert llm.call_count == 0
    assert llm.script_remaining == 0


# ------------------------------------------------------------ failure injection


async def test_it_can_be_told_to_fail() -> None:
    llm = MockLLM(fail_times=2, failure=LLMTimeout)

    with pytest.raises(LLMTimeout):
        await decide(llm, GOAL)
    with pytest.raises(LLMTimeout):
        await decide(llm, GOAL)

    assert (await decide(llm, GOAL, SEARCH_PAGE))["action"]


async def test_it_can_return_malformed_json() -> None:
    """Drives the single repair retry in spec section 4.5 (TEST-006)."""
    llm = MockLLM(malformed_times=1)

    with pytest.raises(LLMBadOutput):
        await decide(llm, GOAL)

    assert (await decide(llm, GOAL, SEARCH_PAGE))["action"]


async def test_failures_are_still_recorded() -> None:
    """A test asserting on retry counts needs the failed attempts too."""
    llm = MockLLM(fail_times=1)

    with pytest.raises(Exception, match="mock failure"):
        await decide(llm, GOAL)

    assert llm.call_count == 1


# ------------------------------------------------------------------ rules


async def test_it_fills_a_field_the_goal_names() -> None:
    decision = await decide(MockLLM(), GOAL, SEARCH_PAGE)

    assert decision["action"] == "type"
    assert decision["target"] == "From"
    assert decision["value"] == "Delhi"


async def test_it_moves_to_the_next_empty_field() -> None:
    filled = SEARCH_PAGE.replace(
        '[0] input[type=text] "From" placeholder="Departure city"',
        '[0] input[type=text] "From" placeholder="Departure city" value="Delhi"',
    )

    decision = await decide(MockLLM(), GOAL, filled)

    assert decision["target"] == "To"
    assert decision["value"] == "Bengaluru"


async def test_it_fills_the_date_from_the_goal() -> None:
    only_date = """URL: http://sky.test/search

[2] input[type=date] "Date"
[3] button "Search flights"
"""

    decision = await decide(MockLLM(), GOAL, only_date)

    assert decision["action"] == "type"
    assert decision["value"] == "20 Oct"


async def test_it_submits_once_the_form_is_filled() -> None:
    full = """URL: http://sky.test/search

[0] input[type=text] "From" value="Delhi"
[1] input[type=text] "To" value="Bengaluru"
[3] button "Search flights"
"""

    decision = await decide(MockLLM(), GOAL, full)

    assert decision["action"] == "click"
    assert decision["target"] == "Search flights"


async def test_it_sorts_by_price_on_the_results_page() -> None:
    decision = await decide(MockLLM(), GOAL, RESULTS_PAGE)

    assert decision["action"] == "click"
    assert decision["target"] == "Cheapest"


async def test_it_does_not_sort_twice() -> None:
    """The URL says it is already sorted, so sorting again would loop."""
    decision = await decide(MockLLM(), GOAL, SORTED_PAGE)

    assert decision["action"] == "extract"


async def test_it_reads_the_results_then_answers_with_the_cheapest() -> None:
    extracted = "extracted: Rs 4,210 AI-812 / Rs 4,980 6E-334 / Rs 5,640 UK-707"

    decision = await decide(MockLLM(), GOAL, extracted, SORTED_PAGE)

    assert decision["action"] == "done"
    assert "4,210" in str(decision["finalAnswer"])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("extracted: Rs 1,200 and Rs 900", "900"),
        ("extracted: ₹5,000 ₹2,500", "2,500"),
        ("extracted: INR 700", "700"),
    ],
)
async def test_it_picks_the_lowest_price_in_any_notation(text: str, expected: str) -> None:
    decision = await decide(MockLLM(), GOAL, text, SORTED_PAGE)

    assert expected in str(decision["finalAnswer"])


async def test_decisions_match_the_spec_schema_shape() -> None:
    decision = await decide(MockLLM(), GOAL, SEARCH_PAGE)

    assert set(decision) == {
        "action",
        "targetIndex",
        "target",
        "value",
        "tabId",
        "reasoning",
        "expectedOutcome",
        "riskLevel",
        "confidence",
        "requiresApproval",
        "needsVision",
        "finalAnswer",
    }


async def test_every_decision_carries_an_expected_outcome() -> None:
    """The verifier needs one on every step (spec section 4.7)."""
    for page in (SEARCH_PAGE, RESULTS_PAGE, SORTED_PAGE):
        decision = await decide(MockLLM(), GOAL, page)
        assert decision["expectedOutcome"]


async def test_it_reports_usage_and_latency() -> None:
    response = await MockLLM().generate_json("s", [user(GOAL), user(SEARCH_PAGE)], {})

    assert response.usage.input_tokens > 0
    assert response.usage.output_tokens > 0
    assert response.latency_ms >= 0
    assert response.provider == "mock"


async def test_it_is_deterministic() -> None:
    """Same inputs, same decision, every run."""
    first = await decide(MockLLM(), GOAL, SEARCH_PAGE)
    second = await decide(MockLLM(), GOAL, SEARCH_PAGE)

    assert first == second


async def test_an_unknown_page_still_produces_a_valid_action() -> None:
    """The loop must never be handed nothing."""
    decision = await decide(MockLLM(), "do something", 'URL: http://x.test\n\n[0] div "?"')

    assert decision["action"] in {"extract", "done"}


# ----------------------------------------------------- reading a fare


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("₹4,810 and ₹5,260", ["4,810", "5,260"]),
        ("Rs 4,210", ["4,210"]),
        ("Rs. 1,299.50", ["1,299.50"]),
        ("INR 999", ["999"]),
        # The one that matters: "filters" ends in "rs", and an unanchored
        # pattern read "Apply filters 28 flights" as a fare of 28 rupees and
        # reported it as the cheapest flight on the page.
        ("Apply filters 28 flights", []),
        ("hotels 12 rooms", []),
        ("Cars 4 available", []),
    ],
)
def test_a_fare_needs_a_currency_token_of_its_own(text: str, expected: list[str]) -> None:
    assert _PRICE.findall(text) == expected
