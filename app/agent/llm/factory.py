"""Build the configured LLM client.

One place that knows which providers exist, so nothing else has to import a
vendor module or branch on a provider name. Adding a provider means adding one
entry to :data:`_BUILDERS`.

Every client comes out wrapped in :class:`RetryingLLM`, so the backoff policy
is the same everywhere and cannot be quietly reimplemented per provider.
"""

from collections.abc import Callable
from typing import Final

from app.agent.llm.base import FallbackLLM, LLMClient, RetryingLLM
from app.agent.llm.mock import MockLLM
from app.core.config import Settings
from app.core.logging import get_logger

__all__ = ["KNOWN_PROVIDERS", "UnknownProviderError", "build_llm"]

logger = get_logger(__name__)

# Named in spec section 4.1. Listed even before they are implemented, so a
# typo and a not-yet-built provider give different errors.
KNOWN_PROVIDERS: Final = ("mock", "anthropic", "openai", "gemini")


class UnknownProviderError(ValueError):
    """The configured provider is not one we know, or not built yet.

    A configuration error, surfaced at startup rather than on the first task:
    a service that accepts work and then cannot plan is worse than one that
    refuses to boot.
    """


def _build_mock(settings: Settings) -> LLMClient:
    return MockLLM(model_name=settings.LLM_MODEL)


_BUILDERS: Final[dict[str, Callable[[Settings], LLMClient]]] = {
    "mock": _build_mock,
}


def _build_bare(name: str, settings: Settings) -> LLMClient:
    """One client, no retry or fallback wrapping."""
    normalised = name.strip().lower()

    if normalised not in KNOWN_PROVIDERS:
        raise UnknownProviderError(
            f"{name!r} is not a known LLM provider. Choose one of: {', '.join(KNOWN_PROVIDERS)}."
        )

    builder = _BUILDERS.get(normalised)
    if builder is None:
        raise UnknownProviderError(
            f"The {normalised!r} provider is not implemented yet. Use 'mock' for now."
        )

    return builder(settings)


def build_llm(settings: Settings, *, provider: str | None = None) -> LLMClient:
    """The client the planner should use.

    ``LLM_PROVIDER`` defaults to ``mock``, so a fresh checkout runs the whole
    loop with no API key. ``LLM_FALLBACK_PROVIDER`` adds a second provider
    tried only when the first is unreachable, never when the model simply
    answered badly.
    """
    name = (provider or settings.LLM_PROVIDER).strip().lower()
    client: LLMClient = RetryingLLM(_build_bare(name, settings))

    if settings.is_prod and name == "mock":
        # Not fatal, because a production smoke test might genuinely want it.
        # Loud, because the agent would appear to work while doing nothing real.
        logger.error(
            "llm_mock_in_production",
            detail="LLM_PROVIDER is 'mock'; the agent will not make real decisions.",
        )

    fallback = (settings.LLM_FALLBACK_PROVIDER or "").strip().lower()
    if fallback and fallback != name:
        client = FallbackLLM(client, RetryingLLM(_build_bare(fallback, settings)))
        logger.info("llm_fallback_configured", primary=name, fallback=fallback)

    logger.info("llm_configured", provider=name, model=settings.LLM_MODEL)
    return client
