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

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from app.core.config import LogLevel, Settings
from app.core.redaction import REDACTED, redact

__all__ = [
    "REDACTED",
    "bind_request_id",
    "clear_request_context",
    "configure_logging",
    "get_logger",
    "redact_sensitive",
]


def redact_sensitive(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """structlog processor: mask sensitive keys anywhere in the event.

    The rules come from :mod:`app.core.redaction`, shared with the audit trail.
    """
    scrubbed: EventDict = redact(event_dict)
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
