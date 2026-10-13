"""A short outline of the steps a task will probably take.

Shown to the person before the first action, so they know roughly how long the
task is. It is a forecast, not a contract: the agent still chooses each real
step from what the page actually shows (spec section 4.5), because a sign-up
popup or a changed layout will not be in any outline written in advance.

Failure here is never fatal. A task without an outline is still a task.
"""

from typing import Any, Final

from app.agent.llm.base import LLMClient, LLMMessage, MessageRole
from app.core.logging import get_logger

__all__ = ["MAX_OUTLINE_STEPS", "OUTLINE_SCHEMA", "make_outline"]

logger = get_logger(__name__)

MAX_OUTLINE_STEPS: Final = 12
MAX_STEP_CHARS: Final = 120

OUTLINE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The browser steps you expect, in order, one short sentence each.",
        }
    },
    "required": ["steps"],
}

SYSTEM: Final = (
    "You outline how a web browsing task will probably go, before it starts. "
    "List the browser steps in order, one short sentence each (open the site, "
    "search, open a result, read the answer). Count only things done in the "
    "browser; do not include thinking or reporting as steps. Use as few steps "
    f"as the task honestly needs, never more than {MAX_OUTLINE_STEPS}. "
    "Answer with a single JSON object matching the schema."
)


async def make_outline(llm: LLMClient, instruction: str) -> list[str]:
    """The expected steps, or an empty list if none could be produced."""
    try:
        response = await llm.generate_json(
            SYSTEM,
            [LLMMessage(role=MessageRole.USER, text=f"Task: {instruction}")],
            OUTLINE_SCHEMA,
        )
    except Exception as exc:
        logger.warning("outline_failed", error_type=type(exc).__name__)
        return []

    raw = response.parsed.get("steps") if isinstance(response.parsed, dict) else None
    if not isinstance(raw, list):
        return []

    steps = [" ".join(item.split())[:MAX_STEP_CHARS] for item in raw if isinstance(item, str)]
    return [step for step in steps if step][:MAX_OUTLINE_STEPS]
