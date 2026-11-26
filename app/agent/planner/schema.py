"""The planner's structured output (spec section 4.5).

The model answers with exactly this shape and nothing else. Validation here is
the first of the four guards spec section 4.11 lists: schema, then the action
allow-list, then target existence, then the risk policy. Each is independent,
and each assumes the model may be wrong or may have been steered by page
content.

The cross-field rules matter as much as the field types. A ``click`` with no
``targetIndex`` is schema-valid and useless; a ``done`` with no ``finalAnswer``
ends the task having answered nothing.
"""

import re
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from app.agent.browser.actions import ALLOWED_URL_SCHEMES
from app.domain.enums import ActionType, RiskLevel, VerificationMethod

__all__ = [
    "DECISION_JSON_SCHEMA",
    "NEEDS_TARGET",
    "Decision",
    "ExpectedOutcome",
    "decision_json_schema",
]

# Actions that act on a specific element, so an index is required.
NEEDS_TARGET: Final[frozenset[ActionType]] = frozenset(
    {ActionType.CLICK, ActionType.TYPE, ActionType.SELECT}
)
# Actions that carry a value the browser will use.
NEEDS_VALUE: Final[frozenset[ActionType]] = frozenset(
    {ActionType.TYPE, ActionType.SELECT, ActionType.NAVIGATE}
)

MAX_REASONING_CHARS: Final = 500


class ExpectedOutcome(BaseModel):
    """What the model claims will be observably true afterwards.

    Required on every decision, because it is what the deterministic verifier
    in spec section 4.7 checks. A step without one cannot be verified, and an
    unverifiable step is indistinguishable from a failed one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    type: VerificationMethod
    value: str = ""


class Decision(BaseModel):
    """One action the planner proposes.

    Field names are camelCase on the wire, matching spec section 4.5 and the
    section 6.1 event bodies.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        frozen=True,
        extra="forbid",
    )

    action: ActionType
    target_index: int | None = Field(default=None, ge=0)
    # A human-readable description of what was targeted, for the activity log.
    target: str | None = None
    value: str | None = None
    # Press Enter in the field after typing, which is how a search or form is
    # submitted without clicking a suggestion or button that may not survive.
    press_enter: bool = False
    tab_id: str | None = None
    reasoning: str = Field(default="", max_length=MAX_REASONING_CHARS)
    expected_outcome: ExpectedOutcome
    risk_level: RiskLevel = RiskLevel.LOW
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    requires_approval: bool = False
    needs_vision: bool = False
    final_answer: str | None = None
    # Old recorded decisions remain readable; provider schema requires an explicit value.
    goal_achieved: bool = Field(default=True, strict=True)
    # Which item of the plan, numbered as the planner was shown it, this action
    # works on. Unchecked here: a number off the end is clamped by the loop,
    # because a slip in bookkeeping is no reason to refuse a sound action.
    plan_step: int | None = None
    # Work the plan does not list that has to happen now (a popup to close, a
    # class to choose), so the plan the person sees keeps up with the agent.
    add_plan_steps: tuple[str, ...] = ()

    # Bookkeeping is read, never refused. A model with no plan in front of it
    # answers `"addPlanSteps": null`, and strict validation turned that into
    # "could not produce a valid decision" and ended a task over a field that
    # changes nothing about the action itself.
    @field_validator("plan_step", mode="before")
    @classmethod
    def _read_plan_step(cls, value: Any) -> int | None:
        """2, 2.0, "2" or "step 2" all mean item 2. Anything else means none."""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int | float):
            return int(value)
        found = re.search(r"\d+", str(value))
        return int(found.group(0)) if found else None

    @field_validator("add_plan_steps", mode="before")
    @classmethod
    def _read_plan_additions(cls, value: Any) -> tuple[str, ...]:
        """Null is none, one bare string is one item, and non-strings are dropped."""
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,) if value.strip() else ()
        if isinstance(value, (list, tuple)):
            return tuple(item for item in value if isinstance(item, str) and item.strip())
        return ()

    @model_validator(mode="after")
    def _check_cross_field_rules(self) -> Self:
        """Rules a type checker cannot express.

        Deliberately raises rather than repairing. A decision that is internally
        inconsistent is a decision the model did not think through, and quietly
        filling in a blank would execute something nobody chose.
        """
        if self.action in NEEDS_TARGET and self.target_index is None:
            raise ValueError(f"{self.action.value!r} requires targetIndex")

        if self.action in NEEDS_VALUE and not (self.value or "").strip():
            raise ValueError(f"{self.action.value!r} requires a value")

        if self.action is ActionType.NAVIGATE:
            url = (self.value or "").strip()
            scheme, separator, _ = url.partition("://")
            if not separator or scheme.lower() not in ALLOWED_URL_SCHEMES:
                raise ValueError("'navigate' requires an http or https URL in value")

        if self.action is ActionType.DONE and not (self.final_answer or "").strip():
            raise ValueError("'done' requires finalAnswer")

        if self.action is ActionType.SWITCH_TAB and not (self.tab_id or "").strip():
            raise ValueError("'switch_tab' requires tabId")

        return self

    @property
    def is_terminal(self) -> bool:
        return self.action is ActionType.DONE


def decision_json_schema() -> dict[str, Any]:
    """The schema handed to a provider for constrained decoding.

    Hand-written rather than taken from ``Decision.model_json_schema()``, for
    two reasons. Strict modes require every property listed in ``required`` with
    nullable types spelled as unions, which Pydantic does not emit. And the
    schema a model sees is a prompt: the descriptions below are instructions,
    and they should read as such rather than being whatever a generator
    produced.

    A test asserts the properties here match the model's fields exactly, so the
    two cannot drift.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "action",
            "targetIndex",
            "target",
            "value",
            "pressEnter",
            "tabId",
            "reasoning",
            "expectedOutcome",
            "riskLevel",
            "confidence",
            "requiresApproval",
            "needsVision",
            "finalAnswer",
            "goalAchieved",
            "planStep",
            "addPlanSteps",
        ],
        "properties": {
            "action": {
                "type": "string",
                "enum": [member.value for member in ActionType],
                "description": "The single action to take next.",
            },
            "targetIndex": {
                "type": ["integer", "null"],
                "description": (
                    "The [n] index of the element to act on, copied from the page state. "
                    "Required for click, type and select. Never invent an index."
                ),
            },
            "target": {
                "type": ["string", "null"],
                "description": "Short description of the targeted element, for the activity log.",
            },
            "value": {
                "type": ["string", "null"],
                "description": (
                    "Text to type, option to select, or the http/https URL to navigate to."
                ),
            },
            "pressEnter": {
                "type": "boolean",
                "description": (
                    "For type only: press Enter after typing. Use it to submit a search box "
                    "or form instead of clicking a suggestion or button. False otherwise."
                ),
            },
            "tabId": {
                "type": ["string", "null"],
                "description": "Tab id from the page state. Required for switch_tab.",
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "One short sentence citing observable evidence from the page state."
                ),
            },
            "expectedOutcome": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "value"],
                "description": "What will be observably true after the action succeeds.",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [member.value for member in VerificationMethod],
                    },
                    "value": {
                        "type": "string",
                        "description": "The expected URL fragment, text or value. May be empty.",
                    },
                },
            },
            "riskLevel": {
                "type": "string",
                "enum": [member.value for member in RiskLevel],
                "description": "HIGH for payment, deletion, or final submission.",
            },
            "confidence": {
                "type": "number",
                "description": "0.0 to 1.0. Below 0.6 triggers visual grounding.",
            },
            "requiresApproval": {
                "type": "boolean",
                "description": "True for any irreversible action. A human decides before it runs.",
            },
            "needsVision": {
                "type": "boolean",
                "description": "True when the page state is not enough to choose a target.",
            },
            "finalAnswer": {
                "type": ["string", "null"],
                "description": "The answer to the goal. Required for done, null otherwise.",
            },
            "goalAchieved": {
                "type": "boolean",
                "description": (
                    "For done: true only when the requested goal is achieved, false if blocked "
                    "or incomplete. Explain the result or blocker in finalAnswer. False otherwise."
                ),
            },
            "planStep": {
                "type": ["integer", "null"],
                "description": (
                    "The number of the PLAN item this action works on. Null when there is no plan."
                ),
            },
            "addPlanSteps": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Work the PLAN does not list that you must do now, before carrying on "
                    "with the item you are on, one short sentence each (this action is the "
                    "first of it). Empty when the plan already covers this action."
                ),
            },
        },
    }


DECISION_JSON_SCHEMA: Final[dict[str, Any]] = decision_json_schema()
