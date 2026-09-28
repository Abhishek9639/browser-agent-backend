"""What a cached flow is made of (spec section 4.12).

The spec is specific about this: "actions referencing role/name locators, not
indices". Indices are assigned fresh by every perception pass, so a sequence
of them describes one reading of one page and nothing else. Replaying them a
day later would aim at whatever now happens to sit at those numbers, which on
a page with a cookie banner is the banner's own buttons.

So a cached step describes its target the way a person would: what kind of
control it is, what it is called, what it says, and only then the CSS path as
a last resort. Re-resolution happens against the live page.

Two things are deliberately not cached.

**A masked value.** The collector masks passwords and card-like fields at
source, so the real value never reaches this layer and never will. The step is
kept as a marker that says the planner has to do this one.

**The final answer.** A cached ``done`` would replay last week's price as
today's, and a confident wrong answer is the one failure a user cannot detect.
Replay therefore never finishes a task by itself: the answer always comes from
the planner reading the live page.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Self

from app.agent.perception.dom_extractor import ElementInfo
from app.agent.perception.state import PageState
from app.agent.planner.schema import Decision
from app.domain.enums import ActionType, VerificationMethod

__all__ = ["Locator", "ReplayStep", "decode_steps", "encode_steps", "hint_for"]

# How many remaining steps to describe to the planner. A hint is a suggestion,
# not a plan, and a long one crowds out the page it is supposed to help read.
HINT_STEPS = 4


@dataclass(frozen=True, slots=True)
class Locator:
    """How to find a control again, in descending order of durability."""

    role: str = ""
    name: str = ""
    text: str = ""
    css_path: str = ""

    @classmethod
    def of(cls, element: ElementInfo) -> Self:
        return cls(
            role=element.role or element.tag,
            name=element.name,
            text=element.text,
            css_path=element.css_path,
        )

    @property
    def described(self) -> str:
        """For a hint and for a failure message."""
        return self.name or self.text or self.css_path or self.role

    def resolve(self, state: PageState) -> ElementInfo | None:
        """Find this control on a page as it is now.

        Tried in order: the role and name together, then the name alone, then
        the visible text, then the stored CSS path. Each rung is weaker than
        the one above it, and the first match wins rather than the best one,
        because a page with two controls this description fits equally well is
        a page where guessing between them is the wrong thing to do.
        """
        rungs: tuple[Callable[[ElementInfo], bool], ...] = (
            lambda item: self._same_role(item) and bool(self.name) and item.name == self.name,
            lambda item: bool(self.name) and item.name == self.name,
            lambda item: bool(self.text) and item.text == self.text,
            lambda item: bool(self.css_path) and item.css_path == self.css_path,
        )
        for matches in rungs:
            found = next((item for item in state.elements if matches(item)), None)
            if found is not None:
                return found
        return None

    def _same_role(self, element: ElementInfo) -> bool:
        return bool(self.role) and (element.role or element.tag) == self.role

    def as_dict(self) -> dict[str, str]:
        return {
            "role": self.role,
            "name": self.name,
            "text": self.text,
            "cssPath": self.css_path,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return cls(
            role=str(raw.get("role") or ""),
            name=str(raw.get("name") or ""),
            text=str(raw.get("text") or ""),
            css_path=str(raw.get("cssPath") or ""),
        )


@dataclass(frozen=True, slots=True)
class ReplayStep:
    """One step of a flow that worked, in a form another run can use."""

    action: ActionType
    locator: Locator | None = None
    value: str | None = None
    target: str | None = None
    # What the verifier checked last time. Carried so a replayed step is held
    # to the same standard as the step that was cached, rather than to
    # whatever the default for its action happens to be.
    expected_type: VerificationMethod = VerificationMethod.DOM_CHANGED
    expected_value: str = ""
    # The value was masked, so there is nothing to replay. The planner takes
    # this step and the rest of the flow becomes a hint.
    needs_planner: bool = False

    @property
    def described(self) -> str:
        name = self.action.value.replace("_", " ")
        target = self.target or (self.locator.described if self.locator else "")
        if self.value and not self.needs_planner:
            return f"{name} {self.value!r} into {target!r}" if target else f"{name} {self.value!r}"
        return f"{name} {target!r}" if target else name

    def as_decision(self, element: ElementInfo | None) -> Decision:
        """The decision a planner would have produced for this step.

        Built rather than stored, so a replayed step goes through exactly the
        same validation, action building, risk assessment and verification as
        a planned one. A second execution path for cached steps would be a
        second place for the safety checks to be missing from.
        """
        return Decision.model_validate(
            {
                "action": self.action.value,
                "targetIndex": element.index if element is not None else None,
                "target": self.target or (self.locator.described if self.locator else None),
                "value": self.value,
                "reasoning": "Replaying a step from a flow that worked before.",
                "expectedOutcome": {
                    "type": self.expected_type.value,
                    "value": self.expected_value,
                },
                "riskLevel": "LOW",
                # Not a model's confidence. A cached step is evidence that this
                # worked once, which is worth less than it looks: the page may
                # have changed, and the verifier is what decides.
                "confidence": 0.5,
                "requiresApproval": False,
                "needsVision": False,
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "locator": self.locator.as_dict() if self.locator else None,
            "value": self.value,
            "target": self.target,
            "expected": {"type": self.expected_type.value, "value": self.expected_value},
            "needsPlanner": self.needs_planner,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self | None:
        """Read one stored step back, or ``None`` if it cannot be trusted.

        A row written by an older version, or by hand, must not be able to
        stop a task: an unreadable step is dropped and the flow replays as far
        as it can.
        """
        try:
            action = ActionType(raw["action"])
        except (KeyError, ValueError):
            return None

        expected = raw.get("expected") or {}
        try:
            method = VerificationMethod(expected.get("type", VerificationMethod.DOM_CHANGED))
        except ValueError:
            method = VerificationMethod.DOM_CHANGED

        locator = raw.get("locator")
        return cls(
            action=action,
            locator=Locator.from_dict(locator) if isinstance(locator, dict) else None,
            value=raw.get("value"),
            target=raw.get("target"),
            expected_type=method,
            expected_value=str(expected.get("value") or ""),
            needs_planner=bool(raw.get("needsPlanner")),
        )


def encode_steps(steps: tuple[ReplayStep, ...]) -> list[dict[str, Any]]:
    return [step.as_dict() for step in steps]


def decode_steps(raw: list[dict[str, Any]]) -> tuple[ReplayStep, ...]:
    decoded = (ReplayStep.from_dict(item) for item in raw if isinstance(item, dict))
    return tuple(step for step in decoded if step is not None)


def hint_for(remaining: tuple[ReplayStep, ...]) -> str | None:
    """What to tell the planner about the rest of a flow that stopped.

    Phrased as what happened before, not as what to do now. The planner's
    prompt already says a hint describes what worked previously and must be
    checked against the page in front of it, and the wording here has to agree
    with that or the prompt is arguing with its own input.
    """
    if not remaining:
        return None

    described = [step.described for step in remaining[:HINT_STEPS]]
    more = len(remaining) - len(described)
    tail = f", then {more} more" if more > 0 else ""
    return f"A previous run of this task continued: {'; then '.join(described)}{tail}."
