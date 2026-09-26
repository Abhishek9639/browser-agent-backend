"""Provider-agnostic LLM interface (spec section 4.1).

Everything above this module talks to :class:`LLMClient` and never to a vendor
SDK, which is what lets the planner be tested against a mock with no network
and no quota (spec section 11), and lets a provider be swapped by changing one
setting.

The interface is deliberately narrow: one method that takes a system prompt,
a conversation and a JSON schema, and returns parsed JSON. The agent never
asks a model for prose.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from app.core.logging import get_logger

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "LLMBadOutput",
    "LLMClient",
    "LLMError",
    "LLMMessage",
    "LLMRateLimited",
    "LLMResponse",
    "LLMTimeout",
    "LLMUnavailable",
    "LLMUsage",
    "MessageRole",
    "RetryingLLM",
    "is_retryable",
    "merge_images",
    "parse_json_payload",
]

logger = get_logger(__name__)

DEFAULT_MAX_ATTEMPTS: Final = 3
DEFAULT_BASE_DELAY_S: Final = 0.5
DEFAULT_MAX_DELAY_S: Final = 8.0
DEFAULT_TIMEOUT_S: Final = 60.0


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class LLMMessage:
    """One turn.

    Images are raw JPEG bytes, the same frames the screenshot module produces,
    so a vision call needs no re-encoding. Every provider adapter is
    responsible for turning them into whatever shape its SDK wants.
    """

    role: MessageRole
    text: str
    images: list[bytes] = field(default_factory=list)

    @property
    def has_images(self) -> bool:
        return bool(self.images)


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """Token counts as the provider reported them.

    Provider-reported rather than estimated, because these reach
    ``llm_decisions`` and feed the cost-per-task metric in spec section 9. The
    summarizer's characters-over-four estimate is a budget rail, not an
    invoice.
    """

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """A validated JSON response, plus what it cost to get it."""

    parsed: dict[str, Any]
    raw_text: str
    usage: LLMUsage
    latency_ms: int
    model_name: str
    provider: str


# ------------------------------------------------------------------ errors


class LLMError(Exception):
    """Base for every provider failure."""


class LLMTimeout(LLMError):
    """The provider did not answer in time."""


class LLMRateLimited(LLMError):
    """Quota or requests-per-minute exceeded.

    ``retry_after`` is honoured when the provider sends one: guessing a backoff
    when the server has told us the answer wastes the very quota we are short
    of.
    """

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class LLMUnavailable(LLMError):
    """The provider is down, unreachable, or returned a 5xx."""


class LLMBadOutput(LLMError):
    """The model answered, but not with usable JSON.

    Never retried by :class:`RetryingLLM`. A transport retry re-sends the same
    prompt and would get the same malformed answer while burning quota. The
    repair path for this is the planner's single re-ask with the error fed
    back, per spec section 4.5.
    """


_RETRYABLE: Final = (LLMTimeout, LLMRateLimited, LLMUnavailable)


def is_retryable(error: BaseException) -> bool:
    """Is retrying the same request worth doing?

    Only for failures where nothing about the request was wrong.
    """
    return isinstance(error, _RETRYABLE)


# ------------------------------------------------------------------ client


@runtime_checkable
class LLMClient(Protocol):
    """What the planner depends on."""

    @property
    def provider(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    async def generate_json(
        self,
        system: str,
        messages: Sequence[LLMMessage],
        json_schema: dict[str, Any],
        *,
        images: Sequence[bytes] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> LLMResponse: ...


class RetryingLLM:
    """Retries transport failures with exponential backoff and jitter.

    Jitter matters more than it looks. Without it, several tasks that hit the
    same rate limit retry in lockstep and hit it again together; the delays
    have to be spread out for the backoff to do anything. This is full jitter:
    a uniform pick from zero to the current ceiling.

    Wraps a client rather than living inside each adapter, so every provider
    gets the same policy and none of them can quietly implement their own.
    """

    def __init__(
        self,
        inner: LLMClient,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        base_delay_s: float = DEFAULT_BASE_DELAY_S,
        max_delay_s: float = DEFAULT_MAX_DELAY_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._inner = inner
        self._max_attempts = max_attempts
        self._base_delay_s = base_delay_s
        self._max_delay_s = max_delay_s
        # Injected so tests do not sleep and backoff is reproducible.
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not crypto

    @property
    def provider(self) -> str:
        return self._inner.provider

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    @property
    def inner(self) -> LLMClient:
        return self._inner

    def _delay_for(self, attempt: int, error: BaseException) -> float:
        if isinstance(error, LLMRateLimited) and error.retry_after_s is not None:
            return max(0.0, error.retry_after_s)
        ceiling = min(self._max_delay_s, self._base_delay_s * (2 ** (attempt - 1)))
        return self._rng.uniform(0.0, ceiling)

    async def generate_json(
        self,
        system: str,
        messages: Sequence[LLMMessage],
        json_schema: dict[str, Any],
        *,
        images: Sequence[bytes] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> LLMResponse:
        last: BaseException | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                return await self._inner.generate_json(
                    system,
                    messages,
                    json_schema,
                    images=images,
                    timeout_s=timeout_s,
                )
            except Exception as error:
                if not is_retryable(error):
                    # LLMBadOutput lands here, and so does anything unexpected.
                    # Re-sending an identical prompt would produce an identical
                    # answer at the same cost.
                    raise
                last = error
                if attempt == self._max_attempts:
                    break

                delay = self._delay_for(attempt, error)
                logger.warning(
                    "llm_retrying",
                    provider=self.provider,
                    attempt=attempt,
                    of=self._max_attempts,
                    error_type=type(error).__name__,
                    delay_s=round(delay, 3),
                )
                await self._sleep(delay)

        logger.error(
            "llm_exhausted",
            provider=self.provider,
            attempts=self._max_attempts,
            error_type=type(last).__name__ if last else "unknown",
        )
        raise last if last else LLMUnavailable("The provider could not be reached.")


class FallbackLLM:
    """Try one client, then another.

    Distinct from retrying: a retry re-asks the same provider when the failure
    looked transient, whereas this gives up on a provider entirely. Spec
    section 4.9 asks for graceful degradation when a provider is down, and this
    is the transport-level half of that.
    """

    def __init__(self, primary: LLMClient, fallback: LLMClient) -> None:
        self._primary = primary
        self._fallback = fallback

    @property
    def provider(self) -> str:
        return self._primary.provider

    @property
    def model_name(self) -> str:
        return self._primary.model_name

    async def generate_json(
        self,
        system: str,
        messages: Sequence[LLMMessage],
        json_schema: dict[str, Any],
        *,
        images: Sequence[bytes] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> LLMResponse:
        try:
            return await self._primary.generate_json(
                system, messages, json_schema, images=images, timeout_s=timeout_s
            )
        except LLMError as error:
            if isinstance(error, LLMBadOutput):
                # The provider is healthy and the model answered badly. Asking
                # a different model the same question hides a prompt problem.
                raise
            logger.warning(
                "llm_falling_back",
                primary=self._primary.provider,
                fallback=self._fallback.provider,
                error_type=type(error).__name__,
            )
            return await self._fallback.generate_json(
                system, messages, json_schema, images=images, timeout_s=timeout_s
            )


def merge_images(
    messages: Sequence[LLMMessage],
    images: Sequence[bytes] | None,
) -> list[LLMMessage]:
    """Attach loose images to the last user turn.

    ``generate_json`` accepts images either on a message or as a convenience
    argument. Normalising here means every adapter sees one shape.
    """
    turns = list(messages)
    if not images:
        return turns

    for position in range(len(turns) - 1, -1, -1):
        if turns[position].role is MessageRole.USER:
            turn = turns[position]
            turns[position] = LLMMessage(
                role=turn.role,
                text=turn.text,
                images=[*turn.images, *images],
            )
            return turns

    turns.append(LLMMessage(role=MessageRole.USER, text="", images=list(images)))
    return turns


def parse_json_payload(text: str, *, provider: str) -> dict[str, Any]:
    """Decode what the model returned, or raise :class:`LLMBadOutput`.

    Tolerates a fenced code block, because a provider without schema
    enforcement will sometimes wrap JSON in markdown however firmly the prompt
    asks it not to. Anything else is a bad answer, and the planner's single
    repair retry is the right response.
    """
    import json

    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1] if "\n" in candidate else ""
        candidate = candidate.rsplit("```", 1)[0].strip()

    if not candidate:
        raise LLMBadOutput(f"{provider} returned an empty response.")

    try:
        parsed = json.loads(candidate)
    except ValueError as exc:
        # The text itself is never logged or attached: it is model output
        # derived from page content.
        raise LLMBadOutput(f"{provider} did not return valid JSON.") from exc

    if not isinstance(parsed, dict):
        raise LLMBadOutput(f"{provider} returned {type(parsed).__name__}, expected an object.")
    return parsed


def elapsed_ms(started: float) -> int:
    """Milliseconds since a ``time.perf_counter()`` reading."""
    return int((time.perf_counter() - started) * 1000)
