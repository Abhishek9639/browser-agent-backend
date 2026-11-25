"""A deterministic LLM that never touches the network (spec section 11).

"A mock LLM provider makes the agent loop testable without API quota." That is
the whole brief, and it pulls in two directions, so this has two modes.

**Scripted** returns decisions from a queue, in order. Exact, obvious, and what
most tests want: the test states the decision, the assertions are about what
the loop does with it.

**Rule-based** reads the goal and the page summary and picks something
sensible. Nothing here is intelligent; it is a handful of rules over the
summary format. It exists so the whole loop, perceive, decide, act, verify,
recover, can be exercised end to end, including the SkyFare task from spec
section 12, without an API key or a cent of quota.

Both modes record every call, and either can be told to fail or to answer with
malformed JSON, which is how the retry policy and the section 4.5 repair path
get tested.
"""

import json
import re
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.agent.llm.base import (
    LLMBadOutput,
    LLMMessage,
    LLMResponse,
    LLMUnavailable,
    LLMUsage,
    MessageRole,
    elapsed_ms,
)
from app.agent.perception.dom_extractor import MASK
from app.core.logging import get_logger

__all__ = ["MockLLM", "RecordedCall", "SummaryLine", "parse_summary"]

logger = get_logger(__name__)

# One line of the spec section 4.6 summary.
_LINE: Final = re.compile(
    r"^\[(?P<index>\d+)\]\s+(?P<tag>[a-zA-Z]+)(?:\[type=(?P<type>[^\]]+)\])?(?P<rest>.*)$"
)
_PLACEHOLDER: Final = re.compile(r'placeholder="([^"]*)"')
_VALUE: Final = re.compile(r'value="([^"]*)"')
_HREF: Final = re.compile(r"href=(\S+)")
_QUOTED: Final = re.compile(r'"([^"]*)"')
_FRAME_LINE: Final = re.compile(r"^Frames:\s*(.+)$", re.MULTILINE)
_URL_LINE: Final = re.compile(r"^URL:\s*(\S+)", re.MULTILINE)

# Prices as the demo sites print them.
# The letters have to start a word. Without the lookbehind, "Apply filters 28
# flights" reads as a fare of 28 rupees, because "filters" ends in "rs", and
# the agent then reports it as the cheapest.
_PRICE: Final = re.compile(
    r"(?:(?<![A-Za-z])(?:Rs\.?|INR)|₹)\s?([\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)

# Goal slots. Deliberately small: enough for "from X to Y on DATE".
_FROM: Final = re.compile(r"\bfrom\s+([A-Za-z][\w'\- ]*?)(?=\s+\bto\b|\s*$|,)", re.IGNORECASE)
_TO: Final = re.compile(
    r"\bto\s+([A-Za-z][\w'\- ]*?)(?=\s+\bon\b|\s+\bfor\b|\s*$|,)", re.IGNORECASE
)
_ON: Final = re.compile(r"\bon\s+(\d{1,2}\s+[A-Za-z]+|\d{4}-\d{2}-\d{2})", re.IGNORECASE)

_SEARCH_WORDS: Final = ("search", "submit", "find", "go", "continue", "next")
_SORT_WORDS: Final = ("cheapest", "price", "low to high", "lowest")


@dataclass(frozen=True, slots=True)
class SummaryLine:
    """One parsed element line from the summary."""

    index: int
    tag: str
    input_type: str | None
    name: str
    placeholder: str
    value: str
    href: str

    @property
    def is_field(self) -> bool:
        return self.tag in {"input", "textarea"} and self.input_type not in {
            "submit",
            "button",
            "checkbox",
            "radio",
        }

    @property
    def is_empty(self) -> bool:
        return not self.value

    @property
    def label(self) -> str:
        return (self.name or self.placeholder).strip()


def parse_summary(summary: str) -> list[SummaryLine]:
    """Read the compact summary back into structured lines.

    The mock consumes exactly what a real model would be shown, so a change to
    the summary format that would confuse a model breaks these tests too.
    """
    lines: list[SummaryLine] = []
    for raw in summary.splitlines():
        match = _LINE.match(raw.strip())
        if not match:
            continue
        rest = match.group("rest") or ""

        placeholder = _PLACEHOLDER.search(rest)
        value = _VALUE.search(rest)
        href = _HREF.search(rest)

        # The bare quoted string is the accessible name; strip the keyed ones
        # first so they cannot be mistaken for it.
        bare = _PLACEHOLDER.sub("", _VALUE.sub("", rest))
        name = _QUOTED.search(bare)

        lines.append(
            SummaryLine(
                index=int(match.group("index")),
                tag=match.group("tag").lower(),
                input_type=match.group("type"),
                name=name.group(1) if name else "",
                placeholder=placeholder.group(1) if placeholder else "",
                value=value.group(1) if value else "",
                href=href.group(1) if href else "",
            )
        )
    return lines


@dataclass(slots=True)
class RecordedCall:
    """What a caller asked for. Tests assert on these."""

    system: str
    messages: list[LLMMessage]
    json_schema: dict[str, Any]
    image_count: int

    @property
    def last_user_text(self) -> str:
        for message in reversed(self.messages):
            if message.role is MessageRole.USER:
                return message.text
        return ""


class MockLLM:
    """Deterministic, offline, and able to misbehave on demand."""

    provider_name: Final = "mock"

    def __init__(
        self,
        *,
        script: Sequence[dict[str, Any]] | None = None,
        model_name: str = "mock-1",
        fail_times: int = 0,
        failure: type[Exception] = LLMUnavailable,
        malformed_times: int = 0,
    ) -> None:
        self._script: deque[dict[str, Any]] = deque(script or ())
        self._model_name = model_name
        self._fail_times = fail_times
        self._failure = failure
        self._malformed_times = malformed_times
        self.calls: list[RecordedCall] = []

    # ------------------------------------------------------------ identity

    @property
    def provider(self) -> str:
        return self.provider_name

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def script_remaining(self) -> int:
        return len(self._script)

    def push(self, *decisions: dict[str, Any]) -> None:
        """Queue more scripted decisions."""
        self._script.extend(decisions)

    def reset(self) -> None:
        self.calls.clear()
        self._script.clear()

    # ------------------------------------------------------------ the call

    async def generate_json(
        self,
        system: str,
        messages: Sequence[LLMMessage],
        json_schema: dict[str, Any],
        *,
        images: Sequence[bytes] | None = None,
        timeout_s: float = 60.0,  # noqa: ARG002 - part of the protocol; nothing here can hang
    ) -> LLMResponse:
        started = time.perf_counter()
        inline_images = sum(len(message.images) for message in messages)
        self.calls.append(
            RecordedCall(
                system=system,
                messages=list(messages),
                json_schema=json_schema,
                image_count=inline_images + len(images or ()),
            )
        )

        # Injected failures come first, so a test can exercise the retry policy
        # without caring what the decision would have been.
        if self._fail_times > 0:
            self._fail_times -= 1
            raise self._failure(f"mock failure ({self._fail_times} remaining)")

        if self._malformed_times > 0:
            self._malformed_times -= 1
            raise LLMBadOutput("mock returned malformed JSON")

        decision = self._script.popleft() if self._script else self._decide(messages)
        raw = json.dumps(decision)

        return LLMResponse(
            parsed=decision,
            raw_text=raw,
            # Rough but stable, so a test asserting on cost does not depend on
            # a tokenizer.
            usage=LLMUsage(
                input_tokens=sum(len(m.text) for m in messages) // 4,
                output_tokens=len(raw) // 4,
            ),
            latency_ms=elapsed_ms(started),
            model_name=self._model_name,
            provider=self.provider_name,
        )

    # ------------------------------------------------------------ rules

    def _decide(self, messages: Sequence[LLMMessage]) -> dict[str, Any]:
        """Pick a plausible next action from the goal and the page.

        The order is the order a person would work in: fill what is empty,
        submit, narrow the results, read them, answer. Each rule only fires
        when the page still offers it, so the sequence advances on its own
        without the mock keeping any state.
        """
        goal = _first_user_text(messages)
        summary = _last_user_text(messages)
        history = "\n".join(message.text for message in messages)
        elements = parse_summary(summary)
        url = _url_of(summary)

        # Results have already been read: answer with the cheapest.
        prices = _prices_in(history)
        if prices and _has_extracted(messages):
            cheapest = min(prices)
            return _done(f"The cheapest option is Rs {cheapest:,.0f}.")

        # Fill any field whose current value is not what the goal asks for.
        # Not just the empty ones: a search form that arrives with today's
        # date already in it would otherwise be searched on the wrong date,
        # and the answer would be right for a question nobody asked.
        for line in elements:
            if not line.is_field:
                continue
            # Shuffled IDs can conservatively trigger secret masking. A masked
            # value cannot equal plaintext; trust a verified write in history
            # rather than typing it again until the task budget is exhausted.
            if line.value == MASK and re.search(
                rf"^\d+\. type {re.escape(line.label)} -> verified$", history, re.MULTILINE
            ):
                continue
            wanted = _value_for(line, goal)
            if wanted and line.value.strip().casefold() != wanted.casefold():
                return _decision(
                    "type",
                    line.index,
                    line.label,
                    value=wanted,
                    reasoning=f"The goal names {wanted} for {line.label.lower()}.",
                    expected={"type": "value_equals", "value": wanted},
                )

        # Everything the goal mentions is filled, so submit.
        submit = _match(elements, _SEARCH_WORDS, tags={"button"})
        if submit is not None:
            return _decision(
                "click",
                submit.index,
                submit.label,
                reasoning="The form is filled, so submit it.",
                expected={"type": "dom_changed", "value": ""},
            )

        # Narrow to the cheapest, but only once.
        if "sort=price" not in url:
            sort = _match(elements, _SORT_WORDS, tags={"a", "button"})
            if sort is not None:
                return _decision(
                    "click",
                    sort.index,
                    sort.label,
                    reasoning="Sorting by price puts the cheapest option first.",
                    expected={"type": "url_contains", "value": "sort=price"},
                )

        # Read what is on the page.
        if not _has_extracted(messages):
            return _decision(
                "extract",
                None,
                "the results list",
                reasoning="Read the results so the cheapest can be reported.",
                expected={"type": "text_present", "value": ""},
            )

        return _done("Nothing further to do on this page.")


# ------------------------------------------------------------------ helpers


def _first_user_text(messages: Sequence[LLMMessage]) -> str:
    for message in messages:
        if message.role is MessageRole.USER:
            return message.text
    return ""


def _last_user_text(messages: Sequence[LLMMessage]) -> str:
    for message in reversed(messages):
        if message.role is MessageRole.USER:
            return message.text
    return ""


def _url_of(summary: str) -> str:
    """Every URL the header names, joined.

    The frame line as well as the top-level one: on a page that renders into
    an iframe, the sort that was just applied shows up in the frame's URL and
    nowhere else, and reading only the outer one makes the mock click the same
    link forever.
    """
    found = [match.group(1) for match in _URL_LINE.finditer(summary)]
    frames = _FRAME_LINE.search(summary)
    if frames:
        found.append(frames.group(1))
    return " ".join(found)


def _prices_in(text: str) -> list[float]:
    return [float(raw.replace(",", "")) for raw in _PRICE.findall(text)]


def _has_extracted(messages: Sequence[LLMMessage]) -> bool:
    """Has an extract result come back in the conversation yet?"""
    return any("extracted:" in message.text.lower() for message in messages)


def _value_for(line: SummaryLine, goal: str) -> str | None:
    """Map a field onto a slot in the goal.

    Only the three slots the flight task needs. A real model does this by
    understanding; the mock does it by keyword, which is enough to drive the
    loop and honest about being a stand-in.

    The input type is checked before the label, because the labels collide:
    SkyFare calls its date field "Departure" and the practice form calls its
    origin field "Departure city", so matching on the word alone types a city
    name into a date picker.
    """
    if line.input_type == "date":
        match = _ON.search(goal)
        return match.group(1).strip() if match else None

    lowered = line.label.lower()
    if not lowered:
        return None

    if "from" in lowered or "depart" in lowered or "origin" in lowered:
        match = _FROM.search(goal)
        return match.group(1).strip() if match else None
    if lowered.startswith("to") or "arriv" in lowered or "destination" in lowered:
        match = _TO.search(goal)
        return match.group(1).strip() if match else None
    if "date" in lowered or "when" in lowered:
        match = _ON.search(goal)
        return match.group(1).strip() if match else None
    return None


def _match(
    lines: Sequence[SummaryLine],
    words: Sequence[str],
    *,
    tags: set[str],
) -> SummaryLine | None:
    for line in lines:
        if line.tag not in tags:
            continue
        haystack = f"{line.label} {line.href}".lower()
        if any(word in haystack for word in words):
            return line
    return None


def _decision(
    action: str,
    index: int | None,
    target: str,
    *,
    value: str | None = None,
    reasoning: str,
    expected: dict[str, str],
) -> dict[str, Any]:
    """Build a decision in the spec section 4.5 shape."""
    return {
        "action": action,
        "targetIndex": index,
        "target": target,
        "value": value,
        "tabId": None,
        "reasoning": reasoning,
        "expectedOutcome": expected,
        "riskLevel": "LOW",
        "confidence": 0.9,
        "requiresApproval": False,
        "needsVision": False,
        "finalAnswer": None,
    }


def _done(answer: str) -> dict[str, Any]:
    decision = _decision(
        "done",
        None,
        "task complete",
        reasoning="The goal has been satisfied.",
        expected={"type": "text_present", "value": ""},
    )
    decision["finalAnswer"] = answer
    return decision
