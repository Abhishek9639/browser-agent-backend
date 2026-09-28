"""Request-scoped middleware: correlation id, access log and API metrics."""

import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core import metrics
from app.core.logging import bind_request_id, clear_request_context, get_logger

__all__ = ["REQUEST_ID_HEADER", "RequestContextMiddleware", "new_request_id", "route_of"]

logger = get_logger(__name__)

REQUEST_ID_HEADER: Final = "X-Request-ID"
_ID_LENGTH: Final = 12


def new_request_id() -> str:
    """``req_`` plus a short random suffix, matching the spec's ``req_123``."""
    return f"req_{uuid.uuid4().hex[:_ID_LENGTH]}"


def route_of(request: Request) -> str:
    """The matched route's full template, or ``unmatched``.

    Never the raw path of an unmatched request. A metric labelled by
    ``/tasks/<some uuid>`` grows a new time series per task, and one labelled by
    whatever a scanner probed grows one per probe: instrumentation is a
    fashionable way to take down the thing it was meant to watch.

    Built by putting the parameter names back into the path rather than by
    reading the route's own ``path``, because this FastAPI nests included
    routers: the matched route knows itself as ``/health`` and the ``/api/v1``
    in front of it belongs to the routers above. Substituting whole segments
    gets the full template without having to walk that tree.
    """
    if request.scope.get("route") is None:
        return "unmatched"

    path = str(request.scope.get("path") or "/")
    params = request.scope.get("path_params") or {}
    if not params:
        # A static route that matched, so the path is one of ours and bounded.
        return path

    placeholders = {str(value): f"{{{name}}}" for name, value in params.items()}
    return "/".join(placeholders.get(segment, segment) for segment in path.split("/"))


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, bind it for logging, and emit one access line.

    An incoming ``X-Request-ID`` is honoured so a trace can be followed across
    the frontend, this service and payments. The id is echoed on the response
    either way.

    The same timing feeds the ``http_request_duration_seconds`` histogram, so
    the access log and the metric can never disagree about how long a request
    took.
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
            _observe(request, status=500, seconds=duration_ms / 1000)
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
        _observe(request, status=response.status_code, seconds=duration_ms / 1000)
        clear_request_context()
        return response


def _observe(request: Request, *, status: int, seconds: float) -> None:
    """Publish one request to the API metrics.

    Errors get their own counter as well as a labelled bucket in the histogram.
    Both are needed: the histogram answers "how slow", the counter answers "how
    often", and an alert on an error rate should not have to reconstruct it from
    histogram labels.
    """
    route = route_of(request)
    labels = (route, request.method, str(status))
    metrics.http_request_duration_seconds.labels(*labels).observe(seconds)
    if status >= 400:
        metrics.http_errors_total.labels(*labels).inc()
