"""Structured logging.

JSON in prod so logs are machine-readable; a coloured console renderer in dev.
``request_id`` is bound once per request by the middleware and merged into every
event via contextvars, so no call site has to pass it around.

Spec section 10 forbids logging page content, screenshots, credentials and typed
passwords. :func:`redact_sensitive` enforces that centrally: it is the last
processor before rendering, so anything added later in the chain is still
covered.
"""

import logging
import sys
from collections.abc import Sequence
from typing import Any, Final

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from app.core.config import LogLevel, Settings

__all__ = [
    "REDACTED",
    "bind_request_id",
    "clear_request_context",
    "configure_logging",
    "get_logger",
    "redact_sensitive",
]

REDACTED: Final = "[REDACTED]"

# Substring match against the lowercased key. Deliberately broad: over-redacting
# a log line is a cosmetic problem, under-redacting one is a security incident.
SENSITIVE_KEY_PARTS: Final[frozenset[str]] = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "card",
        "cookie",
        "credential",
        "cvc",
        "cvv",
        "jwt",
        "passwd",
        "password",
        "private_key",
        "screenshot",
        "secret",
        "token",
    }
)

_MAX_SCRUB_DEPTH: Final = 6


def _is_sensitive(key: object) -> bool:
    lowered = str(key).lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _scrub(value: Any, depth: int = 0) -> Any:
    """Recursively replace values whose key looks sensitive."""
    if depth >= _MAX_SCRUB_DEPTH:
        return value
    if isinstance(value, dict):
        return {
            key: REDACTED if _is_sensitive(key) else _scrub(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub(item, depth + 1) for item in value)
    return value


def redact_sensitive(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """structlog processor: mask sensitive keys anywhere in the event."""
    scrubbed: EventDict = _scrub(event_dict)
    return scrubbed


def _build_processors(settings: Settings) -> Sequence[Processor]:
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        # Always last before rendering.
        redact_sensitive,
    ]
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if settings.is_prod
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    return [*shared, renderer]


def configure_logging(settings: Settings) -> None:
    """Configure structlog. Safe to call more than once."""
    level = logging.getLevelNamesMapping()[LogLevel(settings.LOG_LEVEL).value]

    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level, force=True)

    structlog.configure(
        processors=list(_build_processors(settings)),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Module-level logger."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def bind_request_id(request_id: str) -> None:
    """Bind the id for the current request; every later event carries it."""
    structlog.contextvars.bind_contextvars(request_id=request_id)


def clear_request_context() -> None:
    """Drop per-request context so ids never leak between requests."""
    structlog.contextvars.clear_contextvars()
