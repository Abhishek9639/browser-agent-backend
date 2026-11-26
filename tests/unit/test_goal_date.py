"""Tests for extract_goal_date and goal_date_note helpers.

These guard the date-understanding fix: when the user says "book train for
23 January", the agent must use 23 January, not today.
"""

from datetime import date

import pytest

from app.agent.planner.planner import extract_goal_date, goal_date_note

# Reference "today" used across all tests so results are deterministic.
TODAY = date(2026, 11, 26)  # Wednesday 26 November 2026


# ---------------------------------------------------------------------------
# extract_goal_date – happy paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "goal, expected",
    [
        # Day-Month only → nearest future occurrence
        ("book train for 23 January", date(2027, 1, 23)),
        ("book a ticket for 1 march", date(2027, 3, 1)),
        # Same month, future day → same year
        ("book for 30 November", date(2026, 11, 30)),
        # Same month, past day → next year
        ("book for 10 November", date(2027, 11, 10)),
        # Exactly today → next year (not today)
        ("travel on 26 November", date(2027, 11, 26)),
        # Day-Month-Year explicit
        ("book train for 23 January 2027", date(2027, 1, 23)),
        ("flight on 5 Mar 2028", date(2028, 3, 5)),
        # Month-Day
        ("January 23 flight", date(2027, 1, 23)),
        ("Mar 5 2028 departure", date(2028, 3, 5)),
        # Ordinal suffix
        ("23rd January", date(2027, 1, 23)),
        ("1st march 2027", date(2027, 3, 1)),
        # Numeric DD/MM/YYYY
        ("book for 23/01/2027", date(2027, 1, 23)),
        ("travel 05-03-2028", date(2028, 3, 5)),
        # ISO YYYY-MM-DD
        ("depart 2027-01-23", date(2027, 1, 23)),
    ],
)
def test_extracts_date(goal: str, expected: date) -> None:
    result = extract_goal_date(goal, today=TODAY)
    assert result == expected, f"goal={goal!r}: got {result}, want {expected}"


# ---------------------------------------------------------------------------
# extract_goal_date – no date in goal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "goal",
    [
        "find the cheapest flight from Delhi to Mumbai",
        "search for hotels in Goa",
        "what is the weather like in Chennai",
    ],
)
def test_returns_none_when_no_date(goal: str) -> None:
    assert extract_goal_date(goal, today=TODAY) is None


# ---------------------------------------------------------------------------
# extract_goal_date – edge cases
# ---------------------------------------------------------------------------


def test_invalid_calendar_date_is_skipped() -> None:
    # 31 February is not a real date; should return None.
    assert extract_goal_date("travel on 31 February", today=TODAY) is None


def test_first_date_wins_when_multiple() -> None:
    # Only the first date should be returned.
    result = extract_goal_date("fly 23 January, return 10 February", today=TODAY)
    assert result == date(2027, 1, 23)


# ---------------------------------------------------------------------------
# goal_date_note
# ---------------------------------------------------------------------------


def test_goal_date_note_contains_resolved_date() -> None:
    note = goal_date_note("book train for 23 January", today=TODAY)
    assert note is not None
    assert "23 January 2027" in note
    assert "BOOKING DATE" in note
    # Must remind the agent not to use today's date.
    assert "26 November 2026" in note


def test_goal_date_note_returns_none_when_no_date() -> None:
    assert goal_date_note("find cheap flights", today=TODAY) is None


def test_booking_note_injected_in_planner_turn() -> None:
    """The BOOKING DATE block appears in _build_turn output when a date is in the goal."""
    from app.agent.llm.mock import MockLLM
    from app.agent.perception.dom_extractor import BoundingBox, ElementInfo
    from app.agent.perception.state import PageState
    from app.agent.planner.planner import Planner

    def _page() -> PageState:
        el = ElementInfo(
            index=0,
            tag="button",
            name="Search",
            bbox=BoundingBox(x=0, y=0, width=10, height=10),
            in_viewport=True,
        )
        return PageState(
            url="http://irctc.co.in",
            title="IRCTC",
            elements=[el],
            summary_text="URL: http://irctc.co.in\nTitle: IRCTC\n\n[0] button \"Search\"",
        )

    planner = Planner(MockLLM(script=[]))
    with_date = planner._build_turn(
        "book train from Delhi to Mumbai on 23 January", _page(), (), None
    )[0].text
    without_date = planner._build_turn(
        "find cheap trains from Delhi to Mumbai", _page(), (), None
    )[0].text

    assert "BOOKING DATE" in with_date
    assert "23 January 2027" in with_date
    assert "BOOKING DATE" not in without_date
