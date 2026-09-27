"""Visual grounding (spec section 4.9).

When the DOM cannot say which element is meant, the picture sometimes can.
Two identical "Select" buttons are identical in the accessibility tree and
obviously different on screen, next to two different prices; a canvas UI has
almost no accessibility tree at all.

The method is set-of-marks: number the candidates on the screenshot, ask a
vision model to read off a number, and check that the number exists. Asking
for a number rather than for coordinates is the whole design. A model reading
a label off a picture is doing the thing it is good at, and the answer can be
checked against the marks we drew; a model producing pixel coordinates is
guessing, and the guess cannot be checked against anything.

Nothing here may fail a task. Spec section 4.9 says a vision provider that is
down degrades to DOM-only, and TEST-010 is the test for it.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Final

from app.agent.llm.base import LLMClient, LLMError, LLMMessage, MessageRole
from app.agent.perception.dom_extractor import ElementInfo
from app.agent.perception.state import PageState
from app.agent.planner.registry import PromptRegistry
from app.agent.vision.marks import MarkedImage, annotate
from app.core.logging import get_logger

__all__ = [
    "GROUNDING_JSON_SCHEMA",
    "VISION_PROMPT_PACKAGE",
    "EvidenceContext",
    "GroundingResult",
    "VisionGrounder",
    "VisionTally",
    "VisualGroundingService",
    "ground_reason",
]

logger = get_logger(__name__)

VISION_PROMPT_PACKAGE: Final = "app.agent.vision.prompts"
VISION_FILENAME: Final = "vision_{version}.md"

# Hand-written for the same reason the planner's is: the descriptions are part
# of the prompt, and a generated schema would bury them.
GROUNDING_JSON_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["markLabel", "point", "confidence", "justification"],
    "properties": {
        "markLabel": {
            "type": ["integer", "null"],
            "description": "The number drawn on the box you are choosing. Null if none fits.",
        },
        "point": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": ["x", "y"],
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
            },
            "description": "Centre of the target in image coordinates. Only when no box fits.",
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "justification": {
            "type": "string",
            "description": "One sentence naming the visible evidence.",
        },
    },
}


@dataclass(frozen=True, slots=True)
class EvidenceContext:
    """Everything the vision model is shown."""

    marked: MarkedImage
    description: str
    candidates: str

    @property
    def has_marks(self) -> bool:
        return bool(self.marked.marks)


@dataclass(frozen=True, slots=True)
class GroundingResult:
    """What grounding concluded, and what it cost."""

    element_index: int | None = None
    point: tuple[int, int] | None = None
    bbox: dict[str, int] | None = None
    confidence: float = 0.0
    justification: str = ""
    used_vision: bool = False
    latency_ms: int = 0
    tokens: int = 0
    error: str | None = None

    @property
    def resolved(self) -> bool:
        """Did it produce something the caller can act on?"""
        return self.element_index is not None or self.point is not None


def ground_reason(
    decision_needs_vision: bool,
    confidence: float,
    state: PageState,
    *,
    threshold: float,
    min_elements: int,
) -> str | None:
    """Why this step should be grounded, or None (spec section 4.9).

    Returned as a phrase rather than a boolean because it is recorded and read
    later: "two elements share a name" and "the page has no markup" are
    different problems, and a vision invocation rate is uninterpretable
    without knowing which one is driving it.
    """
    if decision_needs_vision:
        return "the planner asked for it"
    if confidence < threshold:
        return f"confidence {confidence:.2f} is below {threshold:.2f}"
    if _has_duplicate_names(state.elements):
        return "several elements share the same name"
    if len(state.elements) < min_elements and _looks_non_blank(state):
        return "the page has almost no markup"
    return None


def _has_duplicate_names(elements: list[ElementInfo]) -> bool:
    """Two controls a screen reader cannot tell apart.

    Exactly the SkyFare results page: eight "Select" buttons, identical in the
    accessibility tree and obviously different on screen.
    """
    seen: set[str] = set()
    for element in elements:
        name = element.name.strip().casefold()
        if not name:
            continue
        if name in seen:
            return True
        seen.add(name)
    return False


def _looks_non_blank(state: PageState) -> bool:
    """Is there something on the page, despite there being no controls?

    Without this a genuinely blank page triggers grounding on every step, and
    an agent that has navigated somewhere empty spends its budget photographing
    nothing.
    """
    return len(state.text.strip()) > 80 or bool(state.title.strip())


class VisualGroundingService:
    """Grounds a described target against the picture."""

    def __init__(
        self,
        llm: LLMClient,
        *,
        version: str = "v1",
        max_width: int = 1024,
        max_image_bytes: int = 1_500_000,
        timeout_s: float = 45.0,
    ) -> None:
        self._llm = llm
        self._registry = PromptRegistry(
            version, package=VISION_PROMPT_PACKAGE, filename=VISION_FILENAME
        )
        self._max_width = max_width
        self._max_image_bytes = max_image_bytes
        self._timeout_s = timeout_s

    @property
    def prompt_version(self) -> str:
        return self._registry.version

    def build_evidence_context(
        self, page_state: PageState, screenshot: bytes, description: str
    ) -> EvidenceContext:
        """Number the candidates and write them out alongside the picture.

        The text list is not redundant with the image. It is what lets the
        model tie a number to a name, and it is the only part of the evidence
        that survives a provider quietly downscaling the image past legibility.
        """
        marked = annotate(screenshot, page_state.elements, max_width=self._max_width)
        lines = []
        for mark in marked.marks:
            element = page_state.element_at(mark.element_index)
            label = (element.name or element.text) if element else ""
            tag = element.tag if element else "?"
            lines.append(f"{mark.label}. <{tag}> {label[:60]}".rstrip())

        return EvidenceContext(
            marked=marked,
            description=description.strip(),
            candidates="\n".join(lines) or "(no candidates were visible)",
        )

    def validate_output(self, output: dict[str, Any], context: EvidenceContext) -> GroundingResult:
        """Turn the model's answer into something safe to act on.

        The index check is the one that matters, and it is the same guard the
        planner has: a number that is not on the picture cannot be acted on,
        however confident the model is about it. A point is checked against the
        image bounds for the same reason.
        """
        confidence = float(output.get("confidence") or 0.0)
        justification = str(output.get("justification") or "").strip()

        label = output.get("markLabel")
        if isinstance(label, int):
            mark = context.marked.mark_for(label)
            if mark is None:
                return GroundingResult(
                    used_vision=True,
                    confidence=confidence,
                    justification=justification,
                    error=f"The model chose box {label}, which is not on the picture.",
                )
            return GroundingResult(
                element_index=mark.element_index,
                bbox=_unscale(mark.as_dict(), context.marked.scale),
                point=_unscale_point(mark.centre, context.marked.scale),
                confidence=confidence,
                justification=justification,
                used_vision=True,
            )

        point = output.get("point")
        if isinstance(point, dict):
            x, y = int(point.get("x", -1)), int(point.get("y", -1))
            if not (0 <= x < context.marked.width and 0 <= y < context.marked.height):
                return GroundingResult(
                    used_vision=True,
                    confidence=confidence,
                    justification=justification,
                    error=f"The model returned a point outside the image ({x}, {y}).",
                )
            return GroundingResult(
                point=_unscale_point((x, y), context.marked.scale),
                confidence=confidence,
                justification=justification,
                used_vision=True,
            )

        return GroundingResult(
            used_vision=True,
            confidence=confidence,
            justification=justification,
            error="The model could not identify the target.",
        )

    async def resolve_target(self, context: EvidenceContext) -> GroundingResult:
        """Ask the model, and never raise.

        A provider that is down must degrade to DOM-only rather than fail the
        task, which is spec section 4.9 and TEST-010. The failure is returned
        as a result with an error on it, so the caller records an attempt and
        carries on.
        """
        if not context.has_marks:
            return GroundingResult(error="Nothing on the page could be marked.")

        size = context.marked.size_bytes
        if size > self._max_image_bytes:
            # Refused here rather than discovered at the provider, which would
            # waste the round trip and the step.
            return GroundingResult(error=f"The marked frame is too large ({size} bytes).")

        started = time.monotonic()
        try:
            response = await self._llm.generate_json(
                self._registry.system_prompt,
                [
                    LLMMessage(
                        role=MessageRole.USER,
                        text=(
                            f"TARGET\n{context.description}\n\n"
                            f"CANDIDATES (untrusted page content)\n{context.candidates}"
                        ),
                        images=[context.marked.jpeg_bytes],
                    )
                ],
                GROUNDING_JSON_SCHEMA,
                timeout_s=self._timeout_s,
            )
        except LLMError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            logger.warning(
                "grounding_unavailable",
                error_type=type(exc).__name__,
                latency_ms=elapsed,
            )
            return GroundingResult(
                latency_ms=elapsed, error=f"Vision is unavailable ({type(exc).__name__})."
            )

        result = self.validate_output(response.parsed, context)
        resolved = GroundingResult(
            element_index=result.element_index,
            point=result.point,
            bbox=result.bbox,
            confidence=result.confidence,
            justification=result.justification,
            used_vision=True,
            latency_ms=response.latency_ms,
            tokens=response.usage.input_tokens + response.usage.output_tokens,
            error=result.error,
        )
        logger.info(
            "grounded",
            resolved=resolved.resolved,
            index=resolved.element_index,
            confidence=resolved.confidence,
            latency_ms=resolved.latency_ms,
            tokens=resolved.tokens,
        )
        return resolved


def _unscale(box: dict[str, int], scale: float) -> dict[str, int]:
    """Back to page coordinates.

    The marks were drawn on a downscaled image; everything downstream, the
    step event's bbox and a click by coordinate, works in the page's own
    space.
    """
    if scale == 1.0:
        return box
    return {key: round(value / scale) for key, value in box.items()}


def _unscale_point(point: tuple[int, int], scale: float) -> tuple[int, int]:
    if scale == 1.0:
        return point
    return (round(point[0] / scale), round(point[1] / scale))


@dataclass(slots=True)
class VisionTally:
    """Per-task vision metrics (spec section 9)."""

    calls: int = 0
    latency_ms: int = 0
    tokens: int = 0
    failures: int = 0
    reasons: list[str] = field(default_factory=list)

    def record(self, result: GroundingResult, reason: str) -> None:
        self.calls += 1
        self.latency_ms += result.latency_ms
        self.tokens += result.tokens
        self.reasons.append(reason)
        if result.error is not None:
            self.failures += 1


class VisionGrounder:
    """The :class:`app.agent.contracts.Grounder` backed by a vision model."""

    def __init__(self, service: VisualGroundingService) -> None:
        self._service = service

    async def resolve(
        self,
        decision: Any,
        state: PageState,
        screenshot: bytes,
        reason: str,
    ) -> Any:
        from app.agent.contracts import GroundingOutcome

        description = _describe(decision, state)
        context = self._service.build_evidence_context(state, screenshot, description)
        result = await self._service.resolve_target(context)

        if not result.resolved:
            # Degrade to what the DOM said. The planner's own target may well
            # be right; grounding was a second opinion, not a veto.
            logger.info("grounding_declined", reason=reason, error=result.error)
            return GroundingOutcome(
                decision=decision,
                used_vision=result.used_vision,
                confidence=result.confidence,
                justification=result.justification,
                latency_ms=result.latency_ms,
                tokens=result.tokens,
                reason=reason,
                error=result.error,
            )

        corrected = decision
        if result.element_index is not None and result.element_index != decision.target_index:
            logger.info(
                "grounding_corrected_target",
                was=decision.target_index,
                now=result.element_index,
            )
            corrected = decision.model_copy(update={"target_index": result.element_index})

        return GroundingOutcome(
            decision=corrected,
            used_vision=True,
            bbox=result.bbox,
            point=result.point,
            confidence=result.confidence,
            justification=result.justification,
            latency_ms=result.latency_ms,
            tokens=result.tokens,
            reason=reason,
        )


def _describe(decision: Any, state: PageState) -> str:
    """What to tell the vision model it is looking for.

    The planner's own words first, because they are what the step is about.
    The element's accessible name is added when there is one, since a target
    described as "the second Select button" is much easier to find when the
    picture also says the words on it.
    """
    parts = [part for part in (decision.target, decision.reasoning) if part]
    if decision.target_index is not None:
        element = state.element_at(decision.target_index)
        if element is not None and element.name:
            parts.append(f"The page calls it {element.name!r}.")
    return " ".join(parts) or "the element this step should act on"
