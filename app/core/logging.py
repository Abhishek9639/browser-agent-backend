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
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

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
    "step_context",
    "task_context",
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
        # Not cached. Caching binds each module's logger to the processor chain
        # that existed the first time it logged, which makes the pipeline
        # unobservable: `structlog.testing.capture_logs` sees nothing, and the
        # section 10 guarantee that no page content or credential reaches a log
        # line becomes untestable. A guarantee nobody can check is not one, and
        # the cost of checking it is a dict lookup per log call.
        cache_logger_on_first_use=False,
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


@contextmanager
def task_context(task_id: uuid.UUID | str) -> Iterator[None]:
    """Bind a task id for everything logged inside a run.

    Bound once rather than passed to every call, for the same reason
    ``request_id`` is: spec section 9 wants a task id on the lines that have
    one, and a rule that depends on every future call site remembering an
    argument is a rule that will be broken by the next part.

    Restores whatever was bound before, so an agent run inside a request does
    not wipe the request id on its way out.
    """
    tokens = structlog.contextvars.bind_contextvars(task_id=str(task_id))
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


@contextmanager
def step_context(step_number: int) -> Iterator[None]:
    """Bind a step number for the duration of one step."""
    tokens = structlog.contextvars.bind_contextvars(step_number=step_number)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
