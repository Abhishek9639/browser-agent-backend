"""Groq adapter, and the rate-limit fallback it exists for."""

import json
from typing import Any

import pytest

from app.agent.llm.base import (
    FallbackLLM,
    LLMMessage,
    LLMRateLimited,
    LLMUnavailable,
    MessageRole,
    RetryingLLM,
)
from app.agent.llm.factory import build_llm
from app.agent.llm.groq import DEFAULT_MODEL, DEFAULT_VISION_MODEL, GroqLLM
from app.core.config import Settings

SCHEMA = {"type": "object", "properties": {"action": {"type": "string"}}}
DECISION = {"action": "click"}
JPEG = b"\xff\xd8\xff\xe0fake"


class FakeGroq:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.kwargs: dict[str, Any] = {}
        self._raises = raises
        outer = self

        class _Completions:
            async def create(self, **kwargs: Any) -> Any:
                outer.kwargs = kwargs
                if outer._raises:
                    raise outer._raises
                message = type("M", (), {"content": json.dumps(DECISION)})()
                choice = type("C", (), {"message": message})()
                usage = type("U", (), {"prompt_tokens": 70, "completion_tokens": 10})()
                return type("R", (), {"choices": [choice], "usage": usage})()

        self.chat = type("Chat", (), {"completions": _Completions()})()

    def with_options(self, *, timeout: float) -> "FakeGroq":
        return self


def user(text: str, images: list[bytes] | None = None) -> LLMMessage:
    return LLMMessage(role=MessageRole.USER, text=text, images=images or [])


async def test_groq_uses_json_mode_and_puts_the_schema_in_the_prompt() -> None:
    fake = FakeGroq()
    client = GroqLLM(Settings(), client=fake)

    response = await client.generate_json("be brief", [user("go")], SCHEMA)

    assert response.parsed == DECISION
    assert response.provider == "groq"
    assert fake.kwargs["model"] == DEFAULT_MODEL
    assert fake.kwargs["response_format"] == {"type": "json_object"}
    assert fake.kwargs["messages"][0]["role"] == "system"
    assert json.dumps(SCHEMA) in fake.kwargs["messages"][0]["content"]


async def test_groq_ignores_the_primary_providers_model_name() -> None:
    """A Gemini model name must never reach Groq when it is the fallback."""
    fake = FakeGroq()
    client = GroqLLM(Settings(LLM_MODEL="gemini-something"), client=fake)

    await client.generate_json("s", [user("go")], SCHEMA)

    assert fake.kwargs["model"] == DEFAULT_MODEL


async def test_groq_switches_to_the_vision_model_for_images() -> None:
    fake = FakeGroq()
    client = GroqLLM(Settings(), client=fake)

    await client.generate_json("s", [user("look", [JPEG])], SCHEMA)

    assert fake.kwargs["model"] == DEFAULT_VISION_MODEL
    content = fake.kwargs["messages"][1]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_groq_rate_limit_maps_to_our_error() -> None:
    import httpx
    import openai

    response = httpx.Response(
        429, headers={"retry-after": "7"}, request=httpx.Request("POST", "https://x")
    )
    error = openai.RateLimitError("slow down", response=response, body=None)
    client = GroqLLM(Settings(), client=FakeGroq(raises=error))

    with pytest.raises(LLMRateLimited) as caught:
        await client.generate_json("s", [user("go")], SCHEMA)

    assert caught.value.retry_after_s == 7.0


def test_factory_builds_groq_as_a_fallback_that_skips_rate_limit_retries() -> None:
    client = build_llm(
        Settings(
            LLM_PROVIDER="gemini",
            GOOGLE_API_KEY="g",
            LLM_FALLBACK_PROVIDER="groq",
            GROQ_API_KEY="k",
        )
    )

    assert isinstance(client, FallbackLLM)
    assert client.provider == "gemini"


class _AlwaysLimited:
    provider = "gemini"
    model_name = "m"
    calls = 0

    async def generate_json(self, *args: Any, **kwargs: Any) -> Any:
        type(self).calls += 1
        raise LLMRateLimited("quota", retry_after_s=60.0)


async def test_a_rate_limited_primary_falls_back_without_waiting() -> None:
    slept: list[float] = []

    async def no_sleep(seconds: float) -> None:
        slept.append(seconds)

    groq = GroqLLM(Settings(), client=FakeGroq())
    primary = RetryingLLM(_AlwaysLimited(), retry_rate_limits=False, sleep=no_sleep)
    client = FallbackLLM(primary, groq)

    response = await client.generate_json("s", [user("go")], SCHEMA)

    assert response.provider == "groq"
    assert _AlwaysLimited.calls == 1
    assert slept == []


async def test_non_rate_limit_failures_still_retry_before_falling_back() -> None:
    class _Down:
        provider = "gemini"
        model_name = "m"
        calls = 0

        async def generate_json(self, *args: Any, **kwargs: Any) -> Any:
            type(self).calls += 1
            raise LLMUnavailable("down")

    async def no_sleep(seconds: float) -> None:
        return None

    primary = RetryingLLM(_Down(), retry_rate_limits=False, sleep=no_sleep)

    with pytest.raises(LLMUnavailable):
        await primary.generate_json("s", [user("go")], SCHEMA)

    assert _Down.calls > 1


async def test_a_rate_limited_primary_is_skipped_until_the_cooldown_ends() -> None:
    now = [0.0]

    class Primary:
        provider = "gemini"
        model_name = "m"
        calls = 0

        async def generate_json(self, *args: Any, **kwargs: Any) -> Any:
            type(self).calls += 1
            raise LLMRateLimited("quota")

    Primary.calls = 0
    groq = GroqLLM(Settings(), client=FakeGroq())
    client = FallbackLLM(Primary(), groq, rate_limit_cooldown_s=60.0, clock=lambda: now[0])

    await client.generate_json("s", [user("go")], SCHEMA)
    await client.generate_json("s", [user("go")], SCHEMA)
    assert Primary.calls == 1  # the second call went straight to Groq

    now[0] = 61.0
    await client.generate_json("s", [user("go")], SCHEMA)
    assert Primary.calls == 2  # the cooldown ended, so the primary is tried again


async def test_groq_moves_to_the_next_model_when_one_is_rate_limited() -> None:
    import httpx
    import openai

    response = httpx.Response(429, request=httpx.Request("POST", "https://x"))
    limited = openai.RateLimitError("daily limit", response=response, body=None)
    seen: list[str] = []

    class Picky(FakeGroq):
        def __init__(self) -> None:
            super().__init__()
            outer = self
            original = self.chat.completions.create

            async def create(**kwargs: Any) -> Any:
                seen.append(kwargs["model"])
                if kwargs["model"] == DEFAULT_MODEL:
                    raise limited
                return await original(**kwargs)

            outer.chat.completions.create = create  # type: ignore[method-assign]

    client = GroqLLM(Settings(), client=Picky())

    result = await client.generate_json("s", [user("go")], SCHEMA)

    assert seen == [DEFAULT_MODEL, "openai/gpt-oss-20b"]
    assert result.model_name == "openai/gpt-oss-20b"


async def test_a_rate_limit_that_clears_in_minutes_is_not_slept_through() -> None:
    slept: list[float] = []

    async def no_sleep(seconds: float) -> None:
        slept.append(seconds)

    class DailyLimit:
        provider = "groq"
        model_name = "m"
        calls = 0

        async def generate_json(self, *args: Any, **kwargs: Any) -> Any:
            type(self).calls += 1
            raise LLMRateLimited("daily", retry_after_s=756.0)

    DailyLimit.calls = 0
    client = RetryingLLM(DailyLimit(), sleep=no_sleep)

    with pytest.raises(LLMRateLimited):
        await client.generate_json("s", [user("go")], SCHEMA)

    assert DailyLimit.calls == 1
    assert slept == []


async def test_a_413_is_treated_as_a_limit_so_the_next_model_is_tried() -> None:
    import httpx
    import openai

    response = httpx.Response(413, request=httpx.Request("POST", "https://x"))
    too_big = openai.APIStatusError("too large", response=response, body=None)
    seen: list[str] = []

    class Picky(FakeGroq):
        def __init__(self) -> None:
            super().__init__()
            original = self.chat.completions.create

            async def create(**kwargs: Any) -> Any:
                seen.append(kwargs["model"])
                if kwargs["model"] != "qwen/qwen3.8-27b":
                    raise too_big
                return await original(**kwargs)

            self.chat.completions.create = create  # type: ignore[method-assign]

    client = GroqLLM(Settings(), client=Picky())

    result = await client.generate_json("s", [user("go")], SCHEMA)

    assert seen == [DEFAULT_MODEL, "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]
    assert result.model_name == "qwen/qwen3.8-27b"


async def test_the_output_allowance_is_capped_for_groq() -> None:
    fake = FakeGroq()
    client = GroqLLM(Settings(LLM_MAX_OUTPUT_TOKENS=4096), client=fake)

    await client.generate_json("s", [user("go")], SCHEMA)

    assert fake.kwargs["max_tokens"] == 1024
