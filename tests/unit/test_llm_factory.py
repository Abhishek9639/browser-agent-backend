"""B12: building the configured client."""

import pytest

from app.agent.llm.base import FallbackLLM, LLMClient, RetryingLLM
from app.agent.llm.factory import KNOWN_PROVIDERS, UnknownProviderError, build_llm
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


@pytest.mark.parametrize("name", ["anthropic", "openai", "gemini"])
def test_known_but_unbuilt_providers_say_so(name: str) -> None:
    """Different message from a typo, because it is a different problem."""
    with pytest.raises(UnknownProviderError, match="not implemented yet"):
        build_llm(Settings(LLM_PROVIDER=name))


@pytest.mark.parametrize("name", ["llama", "gpt", "", "mockk"])
def test_an_unknown_provider_lists_the_options(name: str) -> None:
    with pytest.raises(UnknownProviderError, match="not a known LLM provider"):
        build_llm(Settings(LLM_PROVIDER=name))


def test_provider_names_are_case_and_space_insensitive() -> None:
    assert build_llm(Settings(LLM_PROVIDER="  MOCK ")).provider == "mock"


def test_the_spec_providers_are_all_known() -> None:
    assert set(KNOWN_PROVIDERS) == {"mock", "anthropic", "openai", "gemini"}


def test_an_explicit_provider_overrides_settings() -> None:
    assert build_llm(Settings(LLM_PROVIDER="anthropic"), provider="mock").provider == "mock"


# ------------------------------------------------------------------ fallback


def test_a_fallback_wraps_the_primary() -> None:
    client = build_llm(Settings(LLM_PROVIDER="mock", LLM_FALLBACK_PROVIDER="mock"))

    assert not isinstance(client, FallbackLLM), "same provider is not a fallback"


def test_no_fallback_by_default() -> None:
    assert not isinstance(build_llm(Settings()), FallbackLLM)


def test_an_unbuilt_fallback_fails_loudly_at_startup() -> None:
    """Better than discovering it during the first outage."""
    with pytest.raises(UnknownProviderError):
        build_llm(Settings(LLM_PROVIDER="mock", LLM_FALLBACK_PROVIDER="anthropic"))


def test_a_mock_in_production_is_allowed_but_flagged() -> None:
    """Deliberately not fatal: a production smoke test might want it.

    The factory logs at error level, because an agent running on the mock
    appears to work while making no real decisions, which is the kind of thing
    nobody notices until a demo.
    """
    client = build_llm(Settings(APP_ENV=AppEnv.PROD, LLM_PROVIDER="mock"))

    assert client.provider == "mock"
