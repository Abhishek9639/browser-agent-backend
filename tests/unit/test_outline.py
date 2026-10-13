"""The up-front outline of expected steps."""

from typing import Any

from app.agent.llm.mock import MockLLM
from app.agent.planner.outline import MAX_OUTLINE_STEPS, make_outline


async def test_an_outline_is_cleaned_and_capped() -> None:
    steps = ["  Open   the site  ", "", 5, *[f"Step {i}" for i in range(30)]]
    llm = MockLLM(script=[{"steps": steps}])

    outline = await make_outline(llm, "find internships")

    assert outline[0] == "Open the site"
    assert len(outline) == MAX_OUTLINE_STEPS
    assert all(isinstance(step, str) and step for step in outline)


async def test_a_long_step_is_truncated() -> None:
    llm = MockLLM(script=[{"steps": ["x" * 500]}])

    outline = await make_outline(llm, "goal")

    assert len(outline[0]) == 120


async def test_a_model_failure_gives_an_empty_outline_not_an_error() -> None:
    class Broken:
        provider = "broken"
        model_name = "b"

        async def generate_json(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("provider down")

    assert await make_outline(Broken(), "goal") == []  # type: ignore[arg-type]


async def test_an_answer_without_a_list_gives_an_empty_outline() -> None:
    llm = MockLLM(script=[{"steps": "just one string"}])

    assert await make_outline(llm, "goal") == []
