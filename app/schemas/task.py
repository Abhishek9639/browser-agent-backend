"""Task and step schemas (spec section 6).

camelCase on the wire, because the frontend generates its TypeScript from
``/openapi.json``. Every field the frontend needs is declared here with an
example, so what it generates is accurate rather than approximately right.
"""

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Final

from pydantic import Field, field_validator

from app.domain.enums import (
    ActionType,
    RecoveryOutcome,
    RiskLevel,
    StepStatus,
    TaskStatus,
    VerificationMethod,
)
from app.schemas.base import CamelModel

__all__ = [
    "CreateTaskRequest",
    "GroundRequest",
    "GroundResponse",
    "LlmDecisionResponse",
    "RecoveryAttemptResponse",
    "StepDetailResponse",
    "StepResponse",
    "TaskListResponse",
    "TaskResponse",
    "VerificationResponse",
    "normalise_instruction",
]

# Long enough for a real instruction, short enough that nobody pastes a book
# into a prompt that is billed per token.
MIN_INSTRUCTION_LENGTH: Final = 5
MAX_INSTRUCTION_LENGTH: Final = 2000
MAX_START_URL_LENGTH: Final = 2048

_WHITESPACE = re.compile(r"\s+")

EXAMPLE_INSTRUCTION: Final = "Find the cheapest flight from Delhi to Bengaluru on 20 Oct on SkyFare"


def normalise_instruction(value: str) -> str:
    """Trim and collapse runs of whitespace.

    Normalised on the way in so that duplicate detection is a plain comparison
    rather than a fuzzy one: the same request typed with a trailing space, or
    pasted with a line break in it, is the same request.
    """
    return _WHITESPACE.sub(" ", value).strip()


class CreateTaskRequest(CamelModel):
    """Body of ``POST /tasks``."""

    instruction: Annotated[
        str,
        Field(
            min_length=MIN_INSTRUCTION_LENGTH,
            max_length=MAX_INSTRUCTION_LENGTH,
            description="What the agent should do, in plain language.",
            examples=[EXAMPLE_INSTRUCTION],
        ),
    ]
    start_url: Annotated[
        str | None,
        Field(
            default=None,
            max_length=MAX_START_URL_LENGTH,
            description=(
                "Where to begin. Must be http or https and inside the "
                "configured domain allow-list. Omit to start from a blank page."
            ),
            examples=["https://skyfare.localhost:8200/"],
        ),
    ] = None

    @field_validator("instruction", mode="before")
    @classmethod
    def _normalise(cls, value: object) -> object:
        """Normalise before the length constraint, not after.

        The length that matters is the length of what gets stored. Checking
        first would reject an instruction padded with newlines that is well
        inside the limit, and accept two hundred spaces as a five character
        instruction. Doing it here also means the bounds in the OpenAPI schema
        are the real ones, which is what the frontend generates its types from.
        """
        if isinstance(value, str):
            return normalise_instruction(value)
        return value

    @field_validator("start_url")
    @classmethod
    def _check_start_url(cls, value: str | None) -> str | None:
        """Scheme only. Whether the host is allowed depends on settings, which
        a schema cannot see, so that check lives in the service."""
        if value is None:
            return None
        url = value.strip()
        if not url:
            return None
        scheme, separator, _ = url.partition("://")
        if not separator or scheme.lower() not in {"http", "https"}:
            raise ValueError("A start URL must begin with http:// or https://.")
        return url


class GroundRequest(CamelModel):
    """Body of ``POST /tasks/{id}/ground``."""

    description: Annotated[
        str,
        Field(
            min_length=2,
            max_length=400,
            description="What to look for, in plain language.",
            examples=["the Select button next to the cheapest fare"],
        ),
    ]


class GroundResponse(CamelModel):
    """What visual grounding concluded. It does not act on the page."""

    resolved: bool = Field(description="Whether the target was identified at all.")
    element_index: int | None = Field(
        default=None,
        description="Index into the current page state, if a marked element matched.",
        examples=[7],
    )
    point: dict[str, int] | None = Field(
        default=None,
        description="Centre of the target in page coordinates.",
        examples=[{"x": 640, "y": 412}],
    )
    bbox: dict[str, int] | None = Field(
        default=None, examples=[{"x": 580, "y": 396, "width": 120, "height": 32}]
    )
    confidence: float = Field(examples=[0.82])
    justification: str = Field(
        default="", examples=["The box sits beside the lowest fare on the page."]
    )
    used_vision: bool
    latency_ms: int = Field(examples=[1840])
    error: str | None = Field(
        default=None,
        description="Set when grounding could not answer. Never fails the task.",
    )


class TaskResponse(CamelModel):
    """One task, as the history and detail screens show it."""

    id: uuid.UUID
    task_number: str = Field(examples=["T-000042"])
    instruction: str = Field(examples=[EXAMPLE_INSTRUCTION])
    status: TaskStatus = Field(examples=[TaskStatus.SUBMITTED])
    start_url: str | None = None
    final_answer: dict[str, Any] | None = Field(
        default=None,
        description="Present once the task completes.",
        examples=[{"text": "Cheapest is IX-2841 at Rs 4,299."}],
    )
    failure_reason: str | None = Field(
        default=None,
        description="Present when the task did not complete.",
    )
    created_at: datetime
    finished_at: datetime | None = None


class TaskListResponse(CamelModel):
    """A page of tasks, plus what the client needs to ask for the next."""

    items: list[TaskResponse]
    total: int = Field(examples=[57])
    page: int = Field(examples=[1])
    page_size: int = Field(examples=[20])
    total_pages: int = Field(examples=[3])
    has_next: bool
    has_previous: bool


class StepResponse(CamelModel):
    """One step in the activity log."""

    id: uuid.UUID
    step_number: int = Field(examples=[1])
    action: ActionType = Field(examples=[ActionType.TYPE])
    target: str | None = Field(default=None, examples=["From"])
    target_index: int | None = Field(default=None, examples=[3])
    value_redacted: str | None = Field(
        default=None,
        description="The typed value. Password and card-like fields read as a mask.",
        examples=["Delhi"],
    )
    tab_id: str | None = Field(default=None, examples=["tab-1"])
    selector_used: str | None = Field(default=None, examples=['[data-agent-idx="3"]'])
    bbox: dict[str, int] | None = Field(
        default=None,
        description="Where the target was, in viewport coordinates.",
        examples=[{"x": 120, "y": 240, "width": 180, "height": 32}],
    )
    screenshot_ref: str | None = Field(
        default=None,
        description=(
            "Storage reference, not a URL. Fetch the image from "
            "GET /tasks/{id}/steps/{stepId}/screenshot."
        ),
    )
    status: StepStatus = Field(examples=[StepStatus.VERIFIED])
    created_at: datetime


class LlmDecisionResponse(CamelModel):
    """What the model decided, and what it cost."""

    id: uuid.UUID
    prompt_version: str = Field(examples=["v1"])
    model_name: str = Field(examples=["gemini-3.6-flash"])
    provider: str = Field(examples=["gemini"])
    reasoning_summary: str | None = None
    confidence: float = Field(examples=[0.92])
    risk_level: RiskLevel = Field(examples=[RiskLevel.LOW])
    used_vision: bool
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    created_at: datetime


class VerificationResponse(CamelModel):
    """Whether the step did what it was supposed to."""

    id: uuid.UUID
    expected_outcome: dict[str, Any] = Field(examples=[{"type": "value_equals", "value": "Delhi"}])
    actual_outcome: dict[str, Any] | None = None
    verified: bool
    verification_method: VerificationMethod = Field(examples=[VerificationMethod.VALUE_EQUALS])
    created_at: datetime


class RecoveryAttemptResponse(CamelModel):
    """One attempt from the recovery ladder."""

    id: uuid.UUID
    attempt_number: int = Field(examples=[1])
    strategy_used: str = Field(examples=["rescroll_and_retry"])
    outcome: RecoveryOutcome = Field(examples=[RecoveryOutcome.SUCCEEDED])
    error: str | None = None
    created_at: datetime


class StepDetailResponse(StepResponse):
    """A step with everything recorded about it."""

    llm_decision: LlmDecisionResponse | None = None
    verification: VerificationResponse | None = None
    recovery_attempts: list[RecoveryAttemptResponse] = Field(default_factory=list)
