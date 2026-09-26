"""Anthropic adapter.

Schema-constrained output through ``output_config.format``, so the model is
held to the section 4.5 shape by the API rather than by asking nicely.

API shapes here were checked against the current SDK and documentation, not
recalled: the vendor surfaces move, and a plausible-looking wrong parameter
fails at runtime with someone's key.
"""

import base64
import time
from collections.abc import Sequence
from typing import Any, Final

import anthropic

from app.agent.llm.base import (
    DEFAULT_TIMEOUT_S,
    LLMError,
    LLMMessage,
    LLMRateLimited,
    LLMResponse,
    LLMTimeout,
    LLMUnavailable,
    LLMUsage,
    MessageRole,
    elapsed_ms,
    merge_images,
    parse_json_payload,
)
from app.core.config import Settings
from app.core.logging import get_logger

__all__ = ["AnthropicLLM"]

logger = get_logger(__name__)

PROVIDER: Final = "anthropic"
# Used when LLM_MODEL is unset. A Sonnet: capable enough to plan, cheap enough for 25 steps a task.
DEFAULT_MODEL: Final = "claude-sonnet-5"
MAX_OUTPUT_TOKENS: Final = 4096


class AnthropicLLM:
    """Claude, through the official async SDK."""

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        key = settings.ANTHROPIC_API_KEY
        self._model = settings.LLM_MODEL or DEFAULT_MODEL
        self._max_output_tokens = settings.LLM_MAX_OUTPUT_TOKENS or MAX_OUTPUT_TOKENS
        # Typed Any: the SDK's request parameters are strict TypedDicts, and
        # building them properly would mean importing a dozen param types for
        # no safety we do not already get from our own schema. It is also the
        # seam the tests inject a fake client through.
        self._client: Any = client or anthropic.AsyncAnthropic(
            api_key=key.get_secret_value() if key else None,
            # Our own RetryingLLM owns the backoff policy, so the SDK's is off.
            # Two independent retry layers multiply into nine attempts.
            max_retries=0,
        )

    @property
    def provider(self) -> str:
        return PROVIDER

    @property
    def model_name(self) -> str:
        return self._model

    def _content_for(self, message: LLMMessage) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for image in message.images:
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(image).decode("ascii"),
                    },
                }
            )
        if message.text:
            blocks.append({"type": "text", "text": message.text})
        return blocks

    async def generate_json(
        self,
        system: str,
        messages: Sequence[LLMMessage],
        json_schema: dict[str, Any],
        *,
        images: Sequence[bytes] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> LLMResponse:
        turns = merge_images(messages, images)
        payload = [
            {"role": turn.role.value, "content": self._content_for(turn)}
            for turn in turns
            if turn.role is not MessageRole.SYSTEM
        ]

        started = time.perf_counter()
        try:
            response = await self._client.with_options(timeout=timeout_s).messages.create(
                model=self._model,
                max_tokens=self._max_output_tokens,
                system=system,
                messages=payload,
                output_config={"format": {"type": "json_schema", "schema": json_schema}},
            )
        except Exception as exc:
            raise _translate(exc) from exc

        text = "".join(block.text for block in response.content if block.type == "text")
        usage = LLMUsage(
            input_tokens=getattr(response.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(response.usage, "output_tokens", 0) or 0,
        )
        # Sizes and counts only. Never the prompt, never the answer: both are
        # derived from page content (spec section 10).
        logger.info(
            "llm_call",
            provider=PROVIDER,
            model=self._model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            images=sum(len(turn.images) for turn in turns),
        )

        return LLMResponse(
            parsed=parse_json_payload(text, provider=PROVIDER),
            raw_text=text,
            usage=usage,
            latency_ms=elapsed_ms(started),
            model_name=self._model,
            provider=PROVIDER,
        )


def _translate(exc: Exception) -> Exception:
    """Map an SDK failure onto our vocabulary."""
    if isinstance(exc, anthropic.APITimeoutError):
        return LLMTimeout("Anthropic timed out.")
    if isinstance(exc, anthropic.RateLimitError):
        return LLMRateLimited("Anthropic rate limit.", retry_after_s=_retry_after(exc))
    if isinstance(exc, anthropic.APIConnectionError):
        return LLMUnavailable("Could not reach Anthropic.")
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500:
            return LLMUnavailable(f"Anthropic returned {exc.status_code}.")
        # 4xx other than 429 is our mistake: a bad key, a bad model name, a
        # malformed request. Retrying sends the same broken request again, so
        # this is a plain LLMError, which is not retryable.
        return LLMError(f"Anthropic rejected the request ({exc.status_code}).")
    return exc


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    header = getattr(response, "headers", {}).get("retry-after") if response else None
    try:
        return float(header) if header else None
    except (TypeError, ValueError):
        return None
