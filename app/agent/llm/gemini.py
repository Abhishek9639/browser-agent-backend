"""Google Gemini adapter.

Uses ``response_json_schema`` with a JSON mime type, which constrains the model
to our schema directly rather than through a Pydantic conversion. Shapes were
checked against the current SDK and documentation rather than recalled.
"""

import re
import time
from collections.abc import Sequence
from typing import Any, Final

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from app.agent.llm.base import (
    DEFAULT_TIMEOUT_S,
    LLMAccessDenied,
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

__all__ = ["GeminiLLM"]

logger = get_logger(__name__)

PROVIDER: Final = "gemini"
# Used when LLM_MODEL is unset. A flash model: the fast, inexpensive tier.
DEFAULT_MODEL: Final = "gemini-3.6-flash"
TOO_MANY_REQUESTS: Final = 429
NOT_FOUND: Final = 404
DEFAULT_EXHAUSTED_S: Final = 60.0
MAX_EXHAUSTED_S: Final = 3600.0
SERVER_ERROR: Final = 500


class GeminiLLM:
    """Gemini, through the official async surface (``client.aio``)."""

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        key = settings.GOOGLE_API_KEY
        self._model = settings.LLM_MODEL or DEFAULT_MODEL
        # Free-tier quotas are per model (some as low as 20 requests a day), so
        # when one is used up, or unavailable to this account, the next answers.
        self._models = [
            self._model,
            *[
                m.strip()
                for m in settings.GEMINI_FALLBACK_MODELS.split(",")
                if m.strip() and m.strip() != self._model
            ],
        ]
        self._exhausted_until: dict[str, float] = {}
        self._last_model = self._model
        self._client: Any = client or genai.Client(
            api_key=key.get_secret_value() if key else None,
        )

    @property
    def provider(self) -> str:
        return PROVIDER

    @property
    def model_name(self) -> str:
        return self._last_model

    def _contents_for(self, turns: Sequence[LLMMessage]) -> list[Any]:
        """Flatten to the parts list the SDK takes.

        Gemini has no assistant/user envelope for a single-shot call, so turns
        are concatenated in order with images inline where they appeared.
        """
        parts: list[Any] = []
        for turn in turns:
            if turn.role is MessageRole.SYSTEM:
                continue
            for image in turn.images:
                parts.append(genai_types.Part.from_bytes(data=image, mime_type="image/jpeg"))
            if turn.text:
                parts.append(turn.text)
        return parts

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

        config = genai_types.GenerateContentConfig(
            system_instruction=system or None,
            response_mime_type="application/json",
            response_json_schema=json_schema,
            # The SDK takes milliseconds here, unlike the other two.
            http_options=genai_types.HttpOptions(timeout=int(timeout_s * 1000)),
        )

        started = time.perf_counter()
        now = time.monotonic()
        # Skip models known to be used up, but never end up with nothing to try.
        candidates = [m for m in self._models if self._exhausted_until.get(m, 0.0) <= now]
        candidates = candidates or self._models[:1]

        response: Any = None
        model = candidates[0]
        for position, model in enumerate(candidates):
            try:
                response = await self._client.aio.models.generate_content(
                    model=model,
                    contents=self._contents_for(turns),
                    config=config,
                )
                break
            except Exception as exc:
                translated = _translate(exc)
                limited = isinstance(translated, LLMRateLimited)
                missing = getattr(exc, "code", None) == NOT_FOUND
                unavailable = isinstance(translated, LLMUnavailable)
                if isinstance(translated, LLMRateLimited):
                    wait = translated.retry_after_s or DEFAULT_EXHAUSTED_S
                    self._exhausted_until[model] = time.monotonic() + min(wait, MAX_EXHAUSTED_S)
                elif missing:
                    self._exhausted_until[model] = time.monotonic() + MAX_EXHAUSTED_S
                elif unavailable:
                    self._exhausted_until[model] = time.monotonic() + DEFAULT_EXHAUSTED_S
                if (limited or missing or unavailable) and position < len(candidates) - 1:
                    logger.warning(
                        "gemini_model_unavailable", model=model, next=candidates[position + 1]
                    )
                    continue
                # SDK error bodies can echo credentials or request content.
                raise translated from None
        self._last_model = model

        text = getattr(response, "text", "") or ""
        metadata = getattr(response, "usage_metadata", None)
        usage = LLMUsage(
            input_tokens=getattr(metadata, "prompt_token_count", 0) or 0,
            output_tokens=getattr(metadata, "candidates_token_count", 0) or 0,
        )
        logger.info(
            "llm_call",
            provider=PROVIDER,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            images=sum(len(turn.images) for turn in turns),
        )

        return LLMResponse(
            parsed=parse_json_payload(text, provider=PROVIDER),
            raw_text=text,
            usage=usage,
            latency_ms=elapsed_ms(started),
            model_name=model,
            provider=PROVIDER,
        )


_RETRY_IN: Final = re.compile(r"retry in ([0-9]+(?:\.[0-9]+)?)s", re.IGNORECASE)


def _retry_in(exc: Exception) -> float | None:
    """The wait Gemini asks for, from "Please retry in 9.1s" in its message."""
    match = _RETRY_IN.search(str(exc))
    return float(match.group(1)) if match else None


def _translate(exc: Exception) -> Exception:
    """Gemini reports everything as APIError with a status code."""
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return LLMTimeout("Gemini timed out.")
    if isinstance(exc, httpx.TransportError):
        # A dropped connection or a failed DNS lookup: the provider was never
        # reached, which is exactly what retry and fallback are for.
        return LLMUnavailable("Could not reach Gemini.")
    if isinstance(exc, genai_errors.APIError):
        code = getattr(exc, "code", None) or 0
        payload = getattr(exc, "details", None)
        error = payload.get("error", payload) if isinstance(payload, dict) else {}
        details = error.get("details", []) if isinstance(error, dict) else []
        reasons = (
            {
                item.get("reason", "")
                for item in details
                if isinstance(item, dict) and isinstance(item.get("reason", ""), str)
            }
            if isinstance(details, list)
            else set()
        )
        if code in {401, 403} or any(reason.startswith("API_KEY_") for reason in reasons):
            return LLMAccessDenied(
                "Gemini rejected access. Check GOOGLE_API_KEY and its API/project permissions."
            )
        if code == TOO_MANY_REQUESTS:
            return LLMRateLimited("Gemini rate limit.", retry_after_s=_retry_in(exc))
        if code >= SERVER_ERROR:
            return LLMUnavailable(f"Gemini returned {code}.")
        if code:
            return LLMError(f"Gemini rejected the request ({code}).")
        return LLMUnavailable("Could not reach Gemini.")
    if isinstance(exc, ConnectionError | OSError):
        return LLMUnavailable("Could not reach Gemini.")
    return exc
