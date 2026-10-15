"""Questions asked before the task starts."""

from typing import Any

from app.agent.llm.mock import MockLLM
from app.agent.planner.clarify import MAX_OPTIONS, make_questions, with_answers


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
