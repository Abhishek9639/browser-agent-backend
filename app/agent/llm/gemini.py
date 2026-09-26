"""Google Gemini adapter.

Uses ``response_json_schema`` with a JSON mime type, which constrains the model
to our schema directly rather than through a Pydantic conversion. Shapes were
checked against the current SDK and documentation rather than recalled.
"""

import time
from collections.abc import Sequence
from typing import Any, Final

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

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

__all__ = ["GeminiLLM"]

logger = get_logger(__name__)

PROVIDER: Final = "gemini"
# Used when LLM_MODEL is unset. A flash model: the fast, inexpensive tier.
DEFAULT_MODEL: Final = "gemini-3.6-flash"
TOO_MANY_REQUESTS: Final = 429
SERVER_ERROR: Final = 500


class GeminiLLM:
    """Gemini, through the official async surface (``client.aio``)."""

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        key = settings.GOOGLE_API_KEY
        self._model = settings.LLM_MODEL or DEFAULT_MODEL
        self._client: Any = client or genai.Client(
            api_key=key.get_secret_value() if key else None,
        )

    @property
    def provider(self) -> str:
        return PROVIDER

    @property
    def model_name(self) -> str:
        return self._model

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
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=self._contents_for(turns),
                config=config,
            )
        except Exception as exc:
            raise _translate(exc) from exc

        text = getattr(response, "text", "") or ""
        metadata = getattr(response, "usage_metadata", None)
        usage = LLMUsage(
            input_tokens=getattr(metadata, "prompt_token_count", 0) or 0,
            output_tokens=getattr(metadata, "candidates_token_count", 0) or 0,
        )
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
    """Gemini reports everything as APIError with a status code."""
    if isinstance(exc, TimeoutError):
        return LLMTimeout("Gemini timed out.")
    if isinstance(exc, genai_errors.APIError):
        code = getattr(exc, "code", None) or 0
        if code == TOO_MANY_REQUESTS:
            return LLMRateLimited("Gemini rate limit.")
        if code >= SERVER_ERROR:
            return LLMUnavailable(f"Gemini returned {code}.")
        if code:
            return LLMError(f"Gemini rejected the request ({code}).")
        return LLMUnavailable("Could not reach Gemini.")
    if isinstance(exc, ConnectionError | OSError):
        return LLMUnavailable("Could not reach Gemini.")
    return exc
