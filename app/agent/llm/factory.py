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
from app.agent.llm.base import FallbackLLM, LLMClient, PacedLLM, RequestPacer, RetryingLLM
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


# One pacer per provider and interval, shared by every client built for it.
_PACERS: dict[tuple[str, float], RequestPacer] = {}


def _requests_per_minute(name: str, settings: Settings) -> int:
    return {
        "gemini": settings.GEMINI_REQUESTS_PER_MINUTE,
        "groq": settings.GROQ_REQUESTS_PER_MINUTE,
    }.get(name, 0)


def _paced(name: str, client: LLMClient, settings: Settings) -> LLMClient:
    rpm = _requests_per_minute(name, settings)
    if rpm <= 0:
        return client
    # A little over 60/rpm: the provider counts in a sliding window, so landing
    # exactly on the boundary still trips it now and then.
    interval = round(60.0 / rpm + 0.5, 2)
    pacer = _PACERS.setdefault((name, interval), RequestPacer(interval))
    return PacedLLM(client, pacer)


def _build_bare(name: str, settings: Settings) -> LLMClient:
    """One client, no retry or fallback wrapping."""
    normalised = name.strip().lower()

    if normalised not in KNOWN_PROVIDERS:
        raise UnknownProviderError(
            f"{name!r} is not a known LLM provider. Choose one of: {', '.join(KNOWN_PROVIDERS)}."
        )

    return _paced(normalised, _BUILDERS[normalised](settings), settings)


def validate_llm_settings(settings: Settings, *, provider: str | None = None) -> None:
    """Reject unusable provider configuration before accepting work; no API calls."""
    names = [provider or settings.LLM_PROVIDER, settings.LLM_FALLBACK_PROVIDER or ""]
    for index, raw in enumerate(names):
        name = raw.strip().lower()
        if not name and index == 1:
            continue
        if name not in KNOWN_PROVIDERS:
            raise UnknownProviderError(
                f"{raw!r} is not a known LLM provider. Choose one of: {', '.join(KNOWN_PROVIDERS)}."
            )
        if settings.is_prod and name == "mock":
            raise MockInProductionError("The mock provider cannot be used when APP_ENV is 'prod'.")
        key_names = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "gemini": "GOOGLE_API_KEY",
            "groq": "GROQ_API_KEY",
        }
        if name in key_names:
            env_var = key_names[name]
            _require_key(name, getattr(settings, env_var), env_var)


def build_llm(settings: Settings, *, provider: str | None = None) -> LLMClient:
    """The client the planner should use.

    ``LLM_PROVIDER`` defaults to ``mock``, so a fresh checkout runs the whole
    loop with no API key. ``LLM_FALLBACK_PROVIDER`` adds a second provider
    tried only when the first is unreachable, never when the model simply
    answered badly.
    """
    validate_llm_settings(settings, provider=provider)
    name = (provider or settings.LLM_PROVIDER).strip().lower()
    fallback = (settings.LLM_FALLBACK_PROVIDER or "").strip().lower()
    has_fallback = bool(fallback) and fallback != name
    # Allow short rate limit retries (e.g. 2-15s momentary spikes) on primary
    # before falling back. If the quota block is longer, it moves to fallback immediately.
    client: LLMClient = RetryingLLM(
        _build_bare(name, settings),
        retry_rate_limits=True,
        max_retry_wait_s=15.0 if has_fallback else 90.0,
    )

    if has_fallback:
        client = FallbackLLM(client, RetryingLLM(_build_bare(fallback, settings)))
        logger.info("llm_fallback_configured", primary=name, fallback=fallback)

    logger.info("llm_configured", provider=name, model=settings.LLM_MODEL)
    return client
