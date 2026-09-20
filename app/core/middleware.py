"""Request-scoped middleware: correlation id and access log."""

import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.logging import bind_request_id, clear_request_context, get_logger

__all__ = ["REQUEST_ID_HEADER", "RequestContextMiddleware", "new_request_id"]

logger = get_logger(__name__)

REQUEST_ID_HEADER: Final = "X-Request-ID"
_ID_LENGTH: Final = 12


def new_request_id() -> str:
    """``req_`` plus a short random suffix, matching the spec's ``req_123``."""
    return f"req_{uuid.uuid4().hex[:_ID_LENGTH]}"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, bind it for logging, and emit one access line.

    An incoming ``X-Request-ID`` is honoured so a trace can be followed across
    the frontend, this service and payments. The id is echoed on the response
    either way.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "").strip()
        request_id = incoming or new_request_id()

        request.state.request_id = request_id
        clear_request_context()
        bind_request_id(request_id)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # The exception handler builds the body; we only record timing here.
            duration_ms = (time.perf_counter() - started) * 1000
            logger.error(
                "request_completed",
                method=request.method,
                path=request.url.path,
                status=500,
                duration_ms=round(duration_ms, 2),
            )
            clear_request_context()
            raise

        duration_ms = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(duration_ms, 2),
        )
        clear_request_context()
        return response
