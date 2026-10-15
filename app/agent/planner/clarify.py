"""Questions to ask the person before the agent starts.

"Book me a train to Delhi" leaves out the day, the time, the class and the
budget, and a browser agent that guesses them books the wrong train. Asking is
cheaper than undoing. One model call decides which of those matter for this
task and offers the likely answers, so the person mostly taps a choice.

It asks only about what changes the outcome and was not already said, never
asks for a password, a card or a one-time code, and failure is never fatal: a
task that could not be questioned still runs on what it was told.
"""

from dataclasses import dataclass
from typing import Any, Final

from app.agent.llm.base import LLMClient, LLMMessage, MessageRole
from app.core.logging import get_logger

__all__ = ["CLARIFY_SCHEMA", "Question", "make_questions", "with_answers"]

logger = get_logger(__name__)

MAX_OPTIONS: Final = 6
MAX_QUESTION_CHARS: Final = 160
MAX_OPTION_CHARS: Final = 40

CLARIFY_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["question", "options"],
            },
        }
    },
    "required": ["questions"],
}

SYSTEM: Final = (
    "You decide what to ask a person before a web browsing agent starts their "
    "task. Ask only about details that would change which result the agent picks "
    "and that the request does not already say: for a train or flight, the date, "
    "the time of day, the class and the budget; for shopping, the budget, brand "
    "or size; for job or internship applications, the location, the kind of role "
    "and the pay. A plain lookup, a question with one answer, or a request that "
    "already names everything needs no questions: return an empty list. Never "
    "ask for a password, a card number, an OTP or any other secret. Each question "
    "is one short sentence with two to six likely answers as options; the person "
    "can also type their own. Ask the fewest questions that matter, never more "
    "than the limit you are given. Answer with a single JSON object matching the "
    "schema."
)


@dataclass(frozen=True, slots=True)
class Question:
    text: str
    options: tuple[str, ...]


async def make_questions(llm: LLMClient, instruction: str, limit: int) -> list[Question]:
    """What to ask, in order. Empty when nothing needs asking or the model failed."""
    if limit <= 0:
        return []
    try:
        response = await llm.generate_json(
            SYSTEM,
            [LLMMessage(role=MessageRole.USER, text=f"Task: {instruction}\nLimit: {limit}")],
            CLARIFY_SCHEMA,
        )
    except Exception as exc:
        logger.warning("clarify_failed", error_type=type(exc).__name__)
        return []

    raw = response.parsed.get("questions") if isinstance(response.parsed, dict) else None
    if not isinstance(raw, list):
        return []

    questions: list[Question] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("question", "")).split())[:MAX_QUESTION_CHARS]
        options = tuple(
            " ".join(o.split())[:MAX_OPTION_CHARS]
            for o in item.get("options", [])
            if isinstance(o, str) and o.strip()
        )[:MAX_OPTIONS]
        if text:
            questions.append(Question(text=text, options=options))
    return questions[:limit]


def with_answers(instruction: str, answers: list[tuple[str, str]]) -> str:
    """The task as the planner should read it, with what the person said added."""
    if not answers:
        return instruction
    lines = "\n".join(f"- {question} {answer}" for question, answer in answers)
    return f"{instruction}\n\nThe person's answers to your questions (follow them):\n{lines}"
