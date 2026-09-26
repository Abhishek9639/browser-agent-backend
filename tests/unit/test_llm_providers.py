"""B13: the three real provider adapters.

Every test here drives a fake SDK client. **Nothing in this file calls a real
API**, so CI never spends money and never depends on a vendor being up. The
live tests at the bottom are skipped unless LIVE_LLM=1.
"""

import base64
import json
import os
from typing import Any

import pytest

from app.agent.llm.anthropic import AnthropicLLM
from app.agent.llm.base import (
    LLMBadOutput,
    LLMClient,
    LLMError,
    LLMMessage,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
    MessageRole,
)
from app.agent.llm.gemini import GeminiLLM
from app.agent.llm.openai import OpenAILLM
from app.core.config import Settings

SCHEMA = {"type": "object", "properties": {"action": {"type": "string"}}}
DECISION = {"action": "click", "targetIndex": 4}
JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


def settings_with(**overrides: Any) -> Settings:
    return Settings(LLM_MODEL="test-model", **overrides)


def user(text: str, images: list[bytes] | None = None) -> LLMMessage:
    return LLMMessage(role=MessageRole.USER, text=text, images=images or [])


# ------------------------------------------------------------------ fakes


class FakeAnthropic:
    """Enough of the Anthropic async client to exercise the adapter."""

    def __init__(self, *, text: str = "", raises: Exception | None = None) -> None:
        self._text = text or json.dumps(DECISION)
        self._raises = raises
        self.kwargs: dict[str, Any] = {}
        self.timeout: float | None = None

        outer = self

        class _Messages:
            async def create(self, **kwargs: Any) -> Any:
                outer.kwargs = kwargs
                if outer._raises:
                    raise outer._raises
                block = type("Block", (), {"type": "text", "text": outer._text})()
                usage = type("Usage", (), {"input_tokens": 120, "output_tokens": 30})()
                return type("Response", (), {"content": [block], "usage": usage})()

        self.messages = _Messages()

    def with_options(self, *, timeout: float) -> "FakeAnthropic":
        self.timeout = timeout
        return self


class FakeOpenAI:
    def __init__(self, *, text: str = "", raises: Exception | None = None) -> None:
        self._text = text or json.dumps(DECISION)
        self._raises = raises
        self.kwargs: dict[str, Any] = {}
        self.timeout: float | None = None

        outer = self

        class _Responses:
            async def create(self, **kwargs: Any) -> Any:
                outer.kwargs = kwargs
                if outer._raises:
                    raise outer._raises
                usage = type("Usage", (), {"input_tokens": 90, "output_tokens": 20})()
                return type("Response", (), {"output_text": outer._text, "usage": usage})()

        self.responses = _Responses()

    def with_options(self, *, timeout: float) -> "FakeOpenAI":
        self.timeout = timeout
        return self


class FakeGemini:
    def __init__(self, *, text: str = "", raises: Exception | None = None) -> None:
        self._text = text or json.dumps(DECISION)
        self._raises = raises
        self.kwargs: dict[str, Any] = {}

        outer = self

        class _Models:
            async def generate_content(self, **kwargs: Any) -> Any:
                outer.kwargs = kwargs
                if outer._raises:
                    raise outer._raises
                metadata = type(
                    "Usage", (), {"prompt_token_count": 80, "candidates_token_count": 15}
                )()
                return type("Response", (), {"text": outer._text, "usage_metadata": metadata})()

        self.aio = type("Aio", (), {"models": _Models()})()


# ------------------------------------------------------------------ contract


@pytest.mark.parametrize(
    ("factory", "fake"),
    [
        (AnthropicLLM, FakeAnthropic),
        (OpenAILLM, FakeOpenAI),
        (GeminiLLM, FakeGemini),
    ],
)
async def test_every_adapter_satisfies_the_protocol(factory: Any, fake: Any) -> None:
    client = factory(settings_with(), client=fake())

    assert isinstance(client, LLMClient)
    assert client.model_name == "test-model"


@pytest.mark.parametrize(
    ("factory", "fake", "provider"),
    [
        (AnthropicLLM, FakeAnthropic, "anthropic"),
        (OpenAILLM, FakeOpenAI, "openai"),
        (GeminiLLM, FakeGemini, "gemini"),
    ],
)
async def test_a_decision_round_trips(factory: Any, fake: Any, provider: str) -> None:
    client = factory(settings_with(), client=fake())

    response = await client.generate_json("system", [user("goal")], SCHEMA)

    assert response.parsed == DECISION
    assert response.provider == provider
    assert response.usage.input_tokens > 0
    assert response.usage.output_tokens > 0
    assert response.latency_ms >= 0


@pytest.mark.parametrize(
    ("factory", "fake"),
    [(AnthropicLLM, FakeAnthropic), (OpenAILLM, FakeOpenAI), (GeminiLLM, FakeGemini)],
)
async def test_malformed_output_is_a_bad_output_error(factory: Any, fake: Any) -> None:
    """Not retried: the same prompt would produce the same rubbish."""
    client = factory(settings_with(), client=fake(text="I think you should click the button"))

    with pytest.raises(LLMBadOutput):
        await client.generate_json("system", [user("goal")], SCHEMA)


@pytest.mark.parametrize(
    ("factory", "fake"),
    [(AnthropicLLM, FakeAnthropic), (OpenAILLM, FakeOpenAI), (GeminiLLM, FakeGemini)],
)
async def test_a_fenced_code_block_is_tolerated(factory: Any, fake: Any) -> None:
    """Models wrap JSON in markdown however firmly the prompt says not to."""
    fenced = "```json\n" + json.dumps(DECISION) + "\n```"
    client = factory(settings_with(), client=fake(text=fenced))

    response = await client.generate_json("system", [user("goal")], SCHEMA)

    assert response.parsed == DECISION


@pytest.mark.parametrize(
    ("factory", "fake"),
    [(AnthropicLLM, FakeAnthropic), (OpenAILLM, FakeOpenAI), (GeminiLLM, FakeGemini)],
)
async def test_a_json_array_is_rejected(factory: Any, fake: Any) -> None:
    client = factory(settings_with(), client=fake(text="[1, 2, 3]"))

    with pytest.raises(LLMBadOutput, match="expected an object"):
        await client.generate_json("system", [user("goal")], SCHEMA)


# ------------------------------------------------------------------ requests


async def test_anthropic_sends_the_schema_and_system_prompt() -> None:
    fake = FakeAnthropic()
    client = AnthropicLLM(settings_with(), client=fake)

    await client.generate_json("be careful", [user("goal")], SCHEMA, timeout_s=12)

    assert fake.kwargs["system"] == "be careful"
    assert fake.kwargs["output_config"]["format"]["schema"] == SCHEMA
    assert fake.kwargs["output_config"]["format"]["type"] == "json_schema"
    assert fake.timeout == 12


async def test_anthropic_encodes_images_as_base64_jpeg() -> None:
    fake = FakeAnthropic()
    client = AnthropicLLM(settings_with(), client=fake)

    await client.generate_json("s", [user("look", [JPEG])], SCHEMA)

    blocks = fake.kwargs["messages"][0]["content"]
    image = next(block for block in blocks if block["type"] == "image")
    assert image["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(image["source"]["data"]) == JPEG


async def test_openai_uses_strict_json_schema_and_instructions() -> None:
    fake = FakeOpenAI()
    client = OpenAILLM(settings_with(), client=fake)

    await client.generate_json("be careful", [user("goal")], SCHEMA, timeout_s=9)

    assert fake.kwargs["instructions"] == "be careful"
    text_format = fake.kwargs["text"]["format"]
    assert text_format["type"] == "json_schema"
    assert text_format["strict"] is True
    assert text_format["schema"] == SCHEMA
    assert fake.timeout == 9


async def test_openai_encodes_images_as_a_data_url() -> None:
    fake = FakeOpenAI()
    client = OpenAILLM(settings_with(), client=fake)

    await client.generate_json("s", [user("look", [JPEG])], SCHEMA)

    blocks = fake.kwargs["input"][0]["content"]
    image = next(block for block in blocks if block["type"] == "input_image")
    prefix = "data:image/jpeg;base64,"
    assert image["image_url"].startswith(prefix)
    assert base64.b64decode(image["image_url"][len(prefix) :]) == JPEG


async def test_gemini_asks_for_json_against_the_schema() -> None:
    fake = FakeGemini()
    client = GeminiLLM(settings_with(), client=fake)

    await client.generate_json("be careful", [user("goal")], SCHEMA)

    config = fake.kwargs["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == SCHEMA
    assert config.system_instruction == "be careful"


async def test_gemini_sends_images_as_inline_parts() -> None:
    fake = FakeGemini()
    client = GeminiLLM(settings_with(), client=fake)

    await client.generate_json("s", [user("look", [JPEG])], SCHEMA)

    parts = fake.kwargs["contents"]
    inline = [part for part in parts if hasattr(part, "inline_data")]
    assert inline
    assert inline[0].inline_data.mime_type == "image/jpeg"


async def test_loose_images_attach_to_the_last_user_turn() -> None:
    fake = FakeAnthropic()
    client = AnthropicLLM(settings_with(), client=fake)

    await client.generate_json("s", [user("first"), user("second")], SCHEMA, images=[JPEG])

    assert not any(b["type"] == "image" for b in fake.kwargs["messages"][0]["content"])
    assert any(b["type"] == "image" for b in fake.kwargs["messages"][1]["content"])


# ------------------------------------------------------------------ errors


def _response(code: int, headers: dict[str, str] | None = None) -> Any:
    """Enough of an httpx response for the SDK exception constructors."""
    return type(
        "R",
        (),
        {"status_code": code, "headers": headers or {}, "request": type("Req", (), {})()},
    )()


def anthropic_status(code: int) -> Exception:
    import anthropic as sdk

    return sdk.APIStatusError(
        "boom",
        response=_response(code),
        body=None,
    )


def openai_status(code: int) -> Exception:
    import openai as sdk

    return sdk.APIStatusError(
        "boom",
        response=_response(code),
        body=None,
    )


@pytest.mark.parametrize(
    ("code", "expected"),
    [(500, LLMUnavailable), (503, LLMUnavailable), (400, LLMError), (401, LLMError)],
)
async def test_anthropic_status_codes_map_to_our_errors(
    code: int, expected: type[Exception]
) -> None:
    """5xx is worth retrying; a 4xx means the request itself was wrong."""
    client = AnthropicLLM(settings_with(), client=FakeAnthropic(raises=anthropic_status(code)))

    with pytest.raises(expected):
        await client.generate_json("s", [user("goal")], SCHEMA)


@pytest.mark.parametrize(
    ("code", "expected"),
    [(500, LLMUnavailable), (400, LLMError)],
)
async def test_openai_status_codes_map_to_our_errors(code: int, expected: type[Exception]) -> None:
    client = OpenAILLM(settings_with(), client=FakeOpenAI(raises=openai_status(code)))

    with pytest.raises(expected):
        await client.generate_json("s", [user("goal")], SCHEMA)


async def test_anthropic_timeout_maps_to_our_timeout() -> None:
    import anthropic as sdk

    failure = sdk.APITimeoutError(request=type("Req", (), {})())
    client = AnthropicLLM(settings_with(), client=FakeAnthropic(raises=failure))

    with pytest.raises(LLMTimeout):
        await client.generate_json("s", [user("goal")], SCHEMA)


async def test_openai_rate_limit_maps_and_keeps_retry_after() -> None:
    import openai as sdk

    response = _response(429, {"retry-after": "7"})
    failure = sdk.RateLimitError("slow down", response=response, body=None)
    client = OpenAILLM(settings_with(), client=FakeOpenAI(raises=failure))

    with pytest.raises(LLMRateLimited) as caught:
        await client.generate_json("s", [user("goal")], SCHEMA)

    assert caught.value.retry_after_s == 7


@pytest.mark.parametrize(
    ("code", "expected"),
    [(429, LLMRateLimited), (503, LLMUnavailable), (400, LLMError)],
)
async def test_gemini_status_codes_map_to_our_errors(code: int, expected: type[Exception]) -> None:
    from google.genai import errors as genai_errors

    failure = genai_errors.APIError(code, {"message": "boom"})
    client = GeminiLLM(settings_with(), client=FakeGemini(raises=failure))

    with pytest.raises(expected):
        await client.generate_json("s", [user("goal")], SCHEMA)


async def test_no_sdk_exception_escapes_as_itself() -> None:
    """Everything above this layer speaks our vocabulary only.

    Each adapter is handed a connection failure from its *own* SDK, which is
    the only kind it can be expected to recognise.
    """
    import anthropic as anthropic_sdk
    import openai as openai_sdk
    from google.genai import errors as genai_errors

    request = type("Req", (), {})()
    cases = [
        (AnthropicLLM, FakeAnthropic, anthropic_sdk.APIConnectionError(request=request)),
        (OpenAILLM, FakeOpenAI, openai_sdk.APIConnectionError(request=request)),
        (GeminiLLM, FakeGemini, genai_errors.APIError(503, {"message": "down"})),
    ]

    for factory, fake, failure in cases:
        client = factory(settings_with(), client=fake(raises=failure))
        with pytest.raises(LLMUnavailable):
            await client.generate_json("s", [user("goal")], SCHEMA)


# ------------------------------------------------------------------ secrets


async def test_the_prompt_and_answer_are_never_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec section 10: both are derived from untrusted page content.

    Recording the logger rather than capturing stderr, because structlog caches
    its writer and capture at that level is fragile. This asserts the thing
    that matters: what the adapter chooses to pass to the log.
    """
    import app.agent.llm.anthropic as module

    recorded: list[dict[str, Any]] = []

    class Recorder:
        def info(self, event: str, **kwargs: Any) -> None:
            recorded.append({"event": event, **kwargs})

        def warning(self, event: str, **kwargs: Any) -> None:
            recorded.append({"event": event, **kwargs})

    monkeypatch.setattr(module, "logger", Recorder())

    secret_page = "the password is hunter2 and the card is 4111111111111111"
    fake = FakeAnthropic(text=json.dumps({"action": "done", "finalAnswer": secret_page}))
    client = AnthropicLLM(settings_with(), client=fake)

    await client.generate_json("system prompt", [user(secret_page)], SCHEMA)

    blob = json.dumps(recorded)
    assert "hunter2" not in blob
    assert "4111111111111111" not in blob
    assert "system prompt" not in blob
    # Sizes and counts are exactly what should be there.
    assert recorded[0]["input_tokens"] == 120
    assert recorded[0]["output_tokens"] == 30


async def test_token_counts_survive_the_redaction_filter() -> None:
    """They contain "token" but are measurements, and `llm_decisions` uses
    these exact column names."""
    from app.core.redaction import redact

    scrubbed = redact({"input_tokens": 120, "output_tokens": 30, "api_key": "sk-secret"})

    assert scrubbed["input_tokens"] == 120
    assert scrubbed["output_tokens"] == 30
    assert scrubbed["api_key"] != "sk-secret"


# ------------------------------------------------------------------ live


LIVE = os.environ.get("LIVE_LLM") == "1"


@pytest.mark.live
@pytest.mark.skipif(not LIVE, reason="set LIVE_LLM=1 and a provider key to run")
async def test_live_text_round_trip() -> None:
    """Costs money. Never runs in CI."""
    from app.agent.llm.factory import build_llm
    from app.core.config import get_settings

    client = build_llm(get_settings())
    schema = {
        "type": "object",
        "properties": {"colour": {"type": "string"}},
        "required": ["colour"],
        "additionalProperties": False,
    }

    response = await client.generate_json(
        "Answer with JSON only.",
        [user("What colour is a clear daytime sky? Answer as JSON.")],
        schema,
        timeout_s=60,
    )

    assert "colour" in response.parsed
    assert response.usage.input_tokens > 0
