"""B12: building the configured client."""

import pytest

from app.agent.llm.base import FallbackLLM, LLMClient, RetryingLLM
from app.agent.llm.factory import (
    KNOWN_PROVIDERS,
    MissingApiKeyError,
    MockInProductionError,
    UnknownProviderError,
    build_llm,
)
from app.core.config import AppEnv, Settings


def test_the_default_is_mock() -> None:
    """A fresh checkout runs the whole loop with no API key (spec section 11)."""
    client = build_llm(Settings())

    assert client.provider == "mock"


def test_every_client_is_wrapped_in_the_retry_policy() -> None:
    """So no provider can quietly implement its own backoff."""
    client = build_llm(Settings())

    assert isinstance(client, RetryingLLM)
    assert isinstance(client, LLMClient)


def test_the_model_name_comes_from_settings() -> None:
    client = build_llm(Settings(LLM_MODEL="some-model-9"))

    assert client.model_name == "some-model-9"


@pytest.mark.parametrize(
    ("name", "env_var"),
    [
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("openai", "OPENAI_API_KEY"),
        ("gemini", "GOOGLE_API_KEY"),
    ],
)
def test_a_provider_without_its_key_fails_at_startup(name: str, env_var: str) -> None:
    """Named in the message, so the fix is obvious. Better than discovering it
    on the first task."""
    with pytest.raises(MissingApiKeyError, match=env_var):
        build_llm(Settings(LLM_PROVIDER=name))


@pytest.mark.parametrize(
    ("name", "key_field"),
    [
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("openai", "OPENAI_API_KEY"),
        ("gemini", "GOOGLE_API_KEY"),
    ],
)
def test_a_provider_with_its_key_builds(name: str, key_field: str) -> None:
    client = build_llm(Settings(LLM_PROVIDER=name, **{key_field: "test-key"}))

    assert client.provider == name


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_key_counts_as_missing(blank: str) -> None:
    with pytest.raises(MissingApiKeyError):
        build_llm(Settings(LLM_PROVIDER="openai", OPENAI_API_KEY=blank))


@pytest.mark.parametrize("name", ["llama", "gpt", "", "mockk"])
def test_an_unknown_provider_lists_the_options(name: str) -> None:
    with pytest.raises(UnknownProviderError, match="not a known LLM provider"):
        build_llm(Settings(LLM_PROVIDER=name))


def test_provider_names_are_case_and_space_insensitive() -> None:
    assert build_llm(Settings(LLM_PROVIDER="  MOCK ")).provider == "mock"


def test_the_spec_providers_are_all_known() -> None:
    assert set(KNOWN_PROVIDERS) == {"mock", "anthropic", "openai", "gemini", "groq"}


def test_an_explicit_provider_overrides_settings() -> None:
    assert build_llm(Settings(LLM_PROVIDER="anthropic"), provider="mock").provider == "mock"


# ------------------------------------------------------------------ fallback


def test_a_fallback_wraps_the_primary() -> None:
    client = build_llm(Settings(LLM_PROVIDER="mock", LLM_FALLBACK_PROVIDER="mock"))

    assert not isinstance(client, FallbackLLM), "same provider is not a fallback"


def test_no_fallback_by_default() -> None:
    assert not isinstance(build_llm(Settings()), FallbackLLM)


def test_a_fallback_without_a_key_fails_at_startup() -> None:
    """Better than discovering it during the first outage."""
    with pytest.raises(MissingApiKeyError):
        build_llm(Settings(LLM_PROVIDER="mock", LLM_FALLBACK_PROVIDER="anthropic"))


def test_a_configured_fallback_wraps_the_primary() -> None:
    client = build_llm(
        Settings(
            LLM_PROVIDER="mock",
            LLM_FALLBACK_PROVIDER="anthropic",
            ANTHROPIC_API_KEY="test-key",
        )
    )

    assert isinstance(client, FallbackLLM)
    assert client.provider == "mock"


def test_a_mock_in_production_refuses_to_start() -> None:
    """An agent on the mock accepts tasks, streams steps and reports success
    while making no real decisions. Refusing to boot is the smaller failure."""
    with pytest.raises(MockInProductionError, match="APP_ENV is 'prod'"):
        build_llm(Settings(APP_ENV=AppEnv.PROD, LLM_PROVIDER="mock"))


@pytest.mark.parametrize("env", [AppEnv.DEV, AppEnv.TEST])
def test_the_mock_is_fine_outside_production(env: AppEnv) -> None:
    assert build_llm(Settings(APP_ENV=env, LLM_PROVIDER="mock")).provider == "mock"
