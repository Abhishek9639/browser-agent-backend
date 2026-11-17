"""Groq adapter.

Groq serves an OpenAI-compatible Chat Completions API, so this reuses the
``openai`` SDK pointed at Groq's base URL; no extra dependency. JSON mode is
used rather than strict ``json_schema``, because strict schemas are supported
by only some Groq models. The schema is put in the system prompt instead, and
the caller's own validation (spec section 4.1) is what actually enforces it.
"""

import base64
import json
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

__all__ = ["GroqLLM"]

logger = get_logger(__name__)

PROVIDER: Final = "groq"
BASE_URL: Final = "https://api.groq.com/openai/v1"
DEFAULT_MODEL: Final = "openai/gpt-oss-120b"
DEFAULT_VISION_MODEL: Final = "qwen/qwen3.8-27b"
MAX_OUTPUT_TOKENS: Final = 1024


class GroqLLM:
    """Llama and friends on Groq, through the OpenAI-compatible endpoint."""

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        key = settings.GROQ_API_KEY
        # Deliberately not LLM_MODEL: that names the primary provider's model,
        # and a Groq fallback must not be sent a Gemini model name.
        self._model = settings.GROQ_MODEL or DEFAULT_MODEL
        # Groq's daily limits are per model, so when one is used up the next
        # one still has headroom.
        self._text_models = [
            self._model,
            *[
                m.strip()
                for m in settings.GROQ_FALLBACK_MODELS.split(",")
                if m.strip() and m.strip() != self._model
            ],
        ]
        self._vision_model = settings.GROQ_VISION_MODEL or DEFAULT_VISION_MODEL
        # Groq counts the whole output allowance against its per-minute token
        # limit, so an allowance far larger than a decision (about 400 tokens)
        # can push a request over the limit before it has said a word.
        self._max_tokens = min(settings.LLM_MAX_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS)
        self._client: Any = client or openai.AsyncOpenAI(
            api_key=key.get_secret_value() if key else None,
            base_url=BASE_URL,
            # RetryingLLM owns the backoff; two layers would multiply.
            max_retries=0,
        )

    @property
    def provider(self) -> str:
        return PROVIDER

    @property
    def model_name(self) -> str:
        return self._model

    def _content_for(self, message: LLMMessage) -> str | list[dict[str, Any]]:
        if not message.images:
            return message.text
        blocks: list[dict[str, Any]] = []
        if message.text:
            blocks.append({"type": "text", "text": message.text})
        for image in message.images:
            encoded = base64.b64encode(image).decode("ascii")
            blocks.append(
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}
            )
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
        has_images = any(turn.images for turn in turns)
        models = [self._vision_model] if has_images else self._text_models

        instructions = (
            f"{system}\n\nRespond with a single JSON object that matches this "
            f"JSON schema, and nothing else:\n{json.dumps(json_schema)}"
        )
        payload: list[dict[str, Any]] = [{"role": "system", "content": instructions}]
        payload.extend(
            {"role": turn.role.value, "content": self._content_for(turn)}
            for turn in turns
            if turn.role is not MessageRole.SYSTEM
        )

        started = time.perf_counter()
        response: Any = None
        model = models[0]
        for position, model in enumerate(models):
            try:
                response = await self._client.with_options(
                    timeout=timeout_s
                ).chat.completions.create(
                    model=model,
                    messages=payload,
                    response_format={"type": "json_object"},
                    max_tokens=self._max_tokens,
                )
                break
            except Exception as exc:
                translated = _translate(exc)
                has_next = position < len(models) - 1
                if isinstance(translated, LLMRateLimited) and has_next:
                    logger.warning(
                        "groq_model_rate_limited", model=model, next=models[position + 1]
                    )
                    continue
                raise translated from exc

        choices = getattr(response, "choices", None) or []
        text = (choices[0].message.content or "") if choices else ""
        usage = getattr(response, "usage", None)
        counts = LLMUsage(
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )
        logger.info(
            "llm_call",
            provider=PROVIDER,
            model=model,
            input_tokens=counts.input_tokens,
            output_tokens=counts.output_tokens,
            images=sum(len(turn.images) for turn in turns),
        )

        return LLMResponse(
            parsed=parse_json_payload(text, provider=PROVIDER),
            raw_text=text,
            usage=counts,
            latency_ms=elapsed_ms(started),
            model_name=model,
            provider=PROVIDER,
        )


def _translate(exc: Exception) -> Exception:
    if isinstance(exc, openai.APITimeoutError):
        return LLMTimeout("Groq timed out.")
    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimited("Groq rate limit.", retry_after_s=_retry_after(exc))
    if isinstance(exc, openai.APIConnectionError):
        return LLMUnavailable("Could not reach Groq.")
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code == 413:
            # Too large for this model's per-minute token limit. It will not
            # shrink by retrying, but another model may take it.
            return LLMRateLimited("Groq request too large for this model.", retry_after_s=None)
        if exc.status_code >= 500:
            return LLMUnavailable(f"Groq returned {exc.status_code}.")
        return LLMError(f"Groq rejected the request ({exc.status_code}).")
    return exc


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    header = getattr(response, "headers", {}).get("retry-after") if response else None
    try:
        return float(header) if header else None
    except (TypeError, ValueError):
        return None
