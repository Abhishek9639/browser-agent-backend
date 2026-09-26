"""OpenAI adapter.

Uses the Responses API with ``text.format`` json_schema in strict mode, which
is the current way to hold a model to a schema. Shapes were checked against the
current SDK and documentation rather than recalled.
"""

import base64
import time
from collections.abc import Sequence
from typing import Any, Final

import openai

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

__all__ = ["OpenAILLM"]

logger = get_logger(__name__)

PROVIDER: Final = "openai"
SCHEMA_NAME: Final = "agent_decision"


class OpenAILLM:
    """GPT, through the official async SDK."""

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        key = settings.OPENAI_API_KEY
        self._model = settings.LLM_MODEL
        self._client: Any = client or openai.AsyncOpenAI(
            api_key=key.get_secret_value() if key else None,
            # RetryingLLM owns the backoff; two layers would multiply.
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
            encoded = base64.b64encode(image).decode("ascii")
            blocks.append(
                {
                    "type": "input_image",
                    "image_url": f"data:image/jpeg;base64,{encoded}",
                }
            )
        if message.text:
            blocks.append({"type": "input_text", "text": message.text})
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
            response = await self._client.with_options(timeout=timeout_s).responses.create(
                model=self._model,
                # The system prompt is its own parameter here, not a message.
                instructions=system,
                input=payload,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": SCHEMA_NAME,
                        "schema": json_schema,
                        "strict": True,
                    }
                },
            )
        except Exception as exc:
            raise _translate(exc) from exc

        text = getattr(response, "output_text", "") or ""
        usage = getattr(response, "usage", None)
        counts = LLMUsage(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        )
        logger.info(
            "llm_call",
            provider=PROVIDER,
            model=self._model,
            input_tokens=counts.input_tokens,
            output_tokens=counts.output_tokens,
            images=sum(len(turn.images) for turn in turns),
        )

        return LLMResponse(
            parsed=parse_json_payload(text, provider=PROVIDER),
            raw_text=text,
            usage=counts,
            latency_ms=elapsed_ms(started),
            model_name=self._model,
            provider=PROVIDER,
        )


def _translate(exc: Exception) -> Exception:
    if isinstance(exc, openai.APITimeoutError):
        return LLMTimeout("OpenAI timed out.")
    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimited("OpenAI rate limit.", retry_after_s=_retry_after(exc))
    if isinstance(exc, openai.APIConnectionError):
        return LLMUnavailable("Could not reach OpenAI.")
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500:
            return LLMUnavailable(f"OpenAI returned {exc.status_code}.")
        return LLMError(f"OpenAI rejected the request ({exc.status_code}).")
    return exc


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    header = getattr(response, "headers", {}).get("retry-after") if response else None
    try:
        return float(header) if header else None
    except (TypeError, ValueError):
        return None
