"""Build the configured LLM client.

One place that knows which providers exist, so nothing else has to import a
vendor module or branch on a provider name. Adding a provider means adding one
entry to :data:`_BUILDERS`.

Every client comes out wrapped in :class:`RetryingLLM`, so the backoff policy
is the same everywhere and cannot be quietly reimplemented per provider.
"""

from collections.abc import Callable
from typing import Final

from pydantic import SecretStr

from app.agent.llm.anthropic import AnthropicLLM
from app.agent.llm.base import FallbackLLM, LLMClient, RetryingLLM
from app.agent.llm.gemini import GeminiLLM
from app.agent.llm.groq import GroqLLM
from app.agent.llm.mock import MockLLM
from app.agent.llm.openai import OpenAILLM
from app.core.config import Settings
from app.core.logging import get_logger

__all__ = [
    "KNOWN_PROVIDERS",
    "MissingApiKeyError",
    "MockInProductionError",
    "UnknownProviderError",
    "build_llm",
]

logger = get_logger(__name__)

# Named in spec section 4.1. Listed even before they are implemented, so a
# typo and a not-yet-built provider give different errors.
KNOWN_PROVIDERS: Final = ("mock", "anthropic", "openai", "gemini", "groq")


class UnknownProviderError(ValueError):
    """The configured provider is not one we know, or not built yet.

    A configuration error, surfaced at startup rather than on the first task:
    a service that accepts work and then cannot plan is worse than one that
    refuses to boot.
    """


class MockInProductionError(RuntimeError):
    """The mock provider was selected in a production environment.

    Refused at startup. The mock is a test double: it would accept tasks,
    stream steps and report success without ever calling a model.
    """


class MissingApiKeyError(ValueError):
    """The selected provider has no API key configured.

    Raised at startup, not on the first task, for the same reason as the
    others: a service that accepts work it cannot do is worse than one that
    refuses to boot.
    """


def _build_mock(settings: Settings) -> LLMClient:
    return MockLLM(model_name=settings.LLM_MODEL or "mock-1")


def _require_key(name: str, key: SecretStr | None, env_var: str) -> None:
    if key is None or not key.get_secret_value().strip():
        raise MissingApiKeyError(
            f"LLM_PROVIDER is {name!r} but {env_var} is not set. "
            f"Set {env_var}, or use LLM_PROVIDER=mock for local work."
        )


def _build_anthropic(settings: Settings) -> LLMClient:
    _require_key("anthropic", settings.ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY")
    return AnthropicLLM(settings)


def _build_openai(settings: Settings) -> LLMClient:
    _require_key("openai", settings.OPENAI_API_KEY, "OPENAI_API_KEY")
    return OpenAILLM(settings)


def _build_gemini(settings: Settings) -> LLMClient:
    _require_key("gemini", settings.GOOGLE_API_KEY, "GOOGLE_API_KEY")
    return GeminiLLM(settings)


def _build_groq(settings: Settings) -> LLMClient:
    _require_key("groq", settings.GROQ_API_KEY, "GROQ_API_KEY")
    return GroqLLM(settings)


_BUILDERS: Final[dict[str, Callable[[Settings], LLMClient]]] = {
    "mock": _build_mock,
    "anthropic": _build_anthropic,
    "openai": _build_openai,
    "gemini": _build_gemini,
    "groq": _build_groq,
}


def _build_bare(name: str, settings: Settings) -> LLMClient:
    """One client, no retry or fallback wrapping."""
    normalised = name.strip().lower()

    if normalised not in KNOWN_PROVIDERS:
        raise UnknownProviderError(
            f"{name!r} is not a known LLM provider. Choose one of: {', '.join(KNOWN_PROVIDERS)}."
        )

    return _BUILDERS[normalised](settings)


def build_llm(settings: Settings, *, provider: str | None = None) -> LLMClient:
    """The client the planner should use.

    ``LLM_PROVIDER`` defaults to ``mock``, so a fresh checkout runs the whole
    loop with no API key. ``LLM_FALLBACK_PROVIDER`` adds a second provider
    tried only when the first is unreachable, never when the model simply
    answered badly.
    """
    name = (provider or settings.LLM_PROVIDER).strip().lower()
    fallback = (settings.LLM_FALLBACK_PROVIDER or "").strip().lower()
    has_fallback = bool(fallback) and fallback != name
    # With a fallback configured, a rate limit moves on at once instead of
    # sleeping out the primary's retry-after: waiting on a quota that will not
    # clear for a minute is exactly what the fallback is for.
    client: LLMClient = RetryingLLM(_build_bare(name, settings), retry_rate_limits=not has_fallback)

    if settings.is_prod and name == "mock":
        # Fatal, not a warning. An agent running on the mock accepts tasks,
        # streams steps and reports success while making no real decisions,
        # which is far worse than a service that refuses to start.
        raise MockInProductionError(
            "LLM_PROVIDER is 'mock' but APP_ENV is 'prod'. "
            "The agent would appear to work while making no real decisions. "
            "Set a real provider, or set APP_ENV to dev or test."
        )

    if has_fallback:
        client = FallbackLLM(client, RetryingLLM(_build_bare(fallback, settings)))
        logger.info("llm_fallback_configured", primary=name, fallback=fallback)

    logger.info("llm_configured", provider=name, model=settings.LLM_MODEL)
    return client
