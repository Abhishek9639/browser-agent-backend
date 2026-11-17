"""Questions asked before the task starts, and about each page the agent opens."""

from typing import Any

from app.agent.llm.mock import MockLLM
from app.agent.planner.clarify import (
    MAX_OPTIONS,
    make_page_questions,
    make_questions,
    with_answers,
)

CAB_PAGE = '[0] input[type=radio] "One Way" (checked)\n[1] input[type=radio] "Round Trip"'


async def test_the_page_is_read_as_untrusted_and_its_choices_become_questions() -> None:
    llm = MockLLM(
        script=[
            {
                "formForTask": True,
                "questions": [
                    {"question": "One way or round trip?", "options": ["One Way", "Round Trip"]}
                ],
            }
        ]
    )

    found = await make_page_questions(
        llm, "book a cab to Agra airport", CAB_PAGE, limit=3, skipped=["Which car?"]
    )

    assert found.for_task is True
    assert [q.text for q in found.questions] == ["One way or round trip?"]
    sent = llm.calls[0].last_user_text
    assert "<page_state>" in sent
    assert "Round Trip" in sent
    # A question the person already passed on is named, so it is not asked again.
    assert "do not ask them again" in sent
    assert "Which car?" in sent


async def test_a_page_that_is_not_the_task_s_form_says_so() -> None:
    llm = MockLLM(script=[{"formForTask": False, "questions": []}])

    found = await make_page_questions(llm, "book a cab", '[0] input "Search"', limit=3)

    assert found.for_task is False
    assert found.questions == []


async def test_the_person_is_never_asked_for_a_secret() -> None:
    """Whatever the page says to ask. "PIN code" is a postal code and stays."""
    asked = [
        "Enter your card number?",
        "What is the OTP you received?",
        "Your UPI PIN?",
        "Which net banking bank?",
        "What is the pickup PIN code?",
    ]
    llm = MockLLM(
        script=[
            {
                "formForTask": True,
                "questions": [{"question": text, "options": []} for text in asked],
            }
        ]
    )

    found = await make_page_questions(llm, "book a cab", CAB_PAGE, limit=6)

    assert [q.text for q in found.questions] == ["What is the pickup PIN code?"]


async def test_a_page_reading_that_fails_asks_nothing() -> None:
    class Broken:
        provider = "broken"
        model_name = "b"

        async def generate_json(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("provider down")

    found = await make_page_questions(Broken(), "book a cab", CAB_PAGE, limit=3)  # type: ignore[arg-type]

    assert found.for_task is False
    assert found.questions == []
    assert (await make_page_questions(MockLLM(script=[]), "x", CAB_PAGE, limit=0)).questions == []


async def test_questions_are_cleaned_and_capped() -> None:
    llm = MockLLM(
        script=[
            {
                "questions": [
                    {"question": "  Which   class? ", "options": ["3AC", " Sleeper ", "", 7]},
                    {"question": "", "options": ["x"]},
                    {"question": "Time?", "options": [f"o{i}" for i in range(20)]},
                    {"question": "Budget?", "options": []},
                ]
            }
        ]
    )

    questions = await make_questions(llm, "book a train", limit=2)

    assert [q.text for q in questions] == ["Which class?", "Time?"]
    assert questions[0].options == ("3AC", "Sleeper")
    assert len(questions[1].options) == MAX_OPTIONS


async def test_nothing_to_ask_gives_no_questions() -> None:
    llm = MockLLM(script=[{"questions": []}])

    assert await make_questions(llm, "what is 2+2", limit=3) == []


async def test_a_zero_limit_does_not_call_the_model() -> None:
    llm = MockLLM(script=[])

    assert await make_questions(llm, "book a train", limit=0) == []


async def test_a_model_failure_gives_no_questions_not_an_error() -> None:
    class Broken:
        provider = "broken"
        model_name = "b"

        async def generate_json(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("provider down")

    assert await make_questions(Broken(), "book a train", limit=3) == []  # type: ignore[arg-type]


def test_answers_are_folded_into_the_task() -> None:
    text = with_answers("Book a train", [("Which class?", "3AC"), ("When?", "evening")])

    assert text.startswith("Book a train")
    assert "Which class? 3AC" in text
    assert "When? evening" in text
    assert with_answers("Book a train", []) == "Book a train"
