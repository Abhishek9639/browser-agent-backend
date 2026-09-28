"""Response headers and a request size limit (spec section 10).

Two middlewares, both deliberately small.

:class:`SecurityHeadersMiddleware` sets the headers a browser needs to be told
explicitly, because the safe behaviour is not the default for any of them.

:class:`BodySizeLimitMiddleware` is pure ASGI rather than a
``BaseHTTPMiddleware``, because the job is to stop reading a body that is too
large. A middleware that has to buffer the request before it can measure it has
already lost: the memory is spent by the time the limit is checked.
"""

from typing import Final

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import get_logger

__all__ = [
    "API_CSP",
    "DOCS_CSP",
    "BodySizeLimitMiddleware",
    "SecurityHeadersMiddleware",
    "is_docs_path",
]

logger = get_logger(__name__)

# An API answers with JSON. Nothing should load, frame, or submit to it, so
# everything is denied rather than restricted: `default-src 'none'` covers the
# fetch directives, and the three that do not fall back to it are named.
API_CSP: Final = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"

# Swagger UI loads its own script and stylesheet from a CDN and needs inline
# styles. Kept separate rather than loosening the API policy, and only reachable
# at all outside production, where the docs routes are not registered.
DOCS_CSP: Final = (
    "default-src 'none'; "
    "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "img-src 'self' https://fastapi.tiangolo.com data:; "
    "font-src 'self' https://cdn.jsdelivr.net; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)

_DOCS_PATHS: Final[frozenset[str]] = frozenset(
    {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
)


def is_docs_path(path: str) -> bool:
    """Is this one of the interactive documentation routes?

    Matched against a fixed set rather than by prefix. A prefix test would let
    ``/docsomething`` pick up the looser policy, and the looser policy permits a
    CDN script.
    """
    return path in _DOCS_PATHS


class SecurityHeadersMiddleware:
    """Set the headers that browsers do not default to safely.

    * ``X-Content-Type-Options: nosniff`` stops a browser deciding for itself
      that a JSON error body is HTML worth rendering.
    * ``X-Frame-Options: DENY`` and ``frame-ancestors 'none'`` between them
      cover old and current browsers. Neither is redundant: the header is what
      pre-CSP browsers read, and the directive is what current ones prefer.
    * ``Referrer-Policy: no-referrer`` because a task id in a path is not
      something to hand to whatever a user clicks next.
    * ``Cross-Origin-Resource-Policy: same-origin`` so a screenshot cannot be
      embedded by another site even if the ownership check is ever relaxed.
    * HSTS only in production. Sending it from a development server on plain
      HTTP would teach a developer's browser to refuse ``http://localhost``, and
      that lesson lasts for two years.
    """

    def __init__(self, app: ASGIApp, *, is_prod: bool, hsts_max_age: int) -> None:
        self._app = app
        self._is_prod = is_prod
        self._hsts_max_age = hsts_max_age

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        docs = is_docs_path(str(scope.get("path", "")))

        async def with_headers(message: Message) -> None:
            if message["type"] != "http.response.start":
                await send(message)
                return

            headers = MutableHeaders(scope=message)
            headers.setdefault("X-Content-Type-Options", "nosniff")
            headers.setdefault("X-Frame-Options", "DENY")
            headers.setdefault("Referrer-Policy", "no-referrer")
            headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
            headers.setdefault("Content-Security-Policy", DOCS_CSP if docs else API_CSP)
            if self._is_prod:
                headers.setdefault(
                    "Strict-Transport-Security",
                    f"max-age={self._hsts_max_age}; includeSubDomains",
                )
            await send(message)

        await self._app(scope, receive, with_headers)


class BodySizeLimitMiddleware:
    """Refuse a request body larger than the configured limit.

    Both halves are needed. ``Content-Length`` is checked first, because an
    honest client sends it and refusing before reading anything is free. The
    body is then counted as it arrives, because the header is a claim: a chunked
    request has no length at all, and a dishonest one can say whatever it likes.

    The interesting part is what happens when the stream goes over. The
    framework reads the body itself, inside its own validation, and turns
    anything that goes wrong there into a 400 about a malformed body: an
    exception raised from the receive channel never reaches this middleware. So
    the response is corrected on the way out instead. Once the counter has
    tripped, the first ``http.response.start`` is replaced with a 413 and the
    handler's own body is dropped, which is possible precisely because nothing
    has been sent to the client yet.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self._max_bytes:
            await self._refuse(send, reason="declared")
            return

        counted = _CountingReceive(receive, self._max_bytes)
        answered = False

        async def guarded(message: Message) -> None:
            nonlocal answered
            if message["type"] == "http.response.start" and counted.exceeded:
                answered = True
                await self._refuse(send, reason="streamed")
                return
            if answered:
                # The handler's own body, which belongs to a response that was
                # replaced. Dropping it is the point.
                return
            await send(message)

        try:
            await self._app(scope, counted, guarded)
        except _BodyTooLarge:
            # Reached only when the application lets the exception through
            # without answering, which is the honest path and the rare one.
            if not answered:
                await self._refuse(send, reason="streamed")

    async def _refuse(self, send: Send, *, reason: str) -> None:
        logger.warning("request_body_rejected", reason=reason, limit=self._max_bytes)
        body = (
            b'{"error":{"code":"PAYLOAD_TOO_LARGE",'
            b'"message":"That request body is larger than this server accepts."}}'
        )
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class _BodyTooLarge(Exception):
    """Internal: the stream went past the limit."""


class _CountingReceive:
    """Wraps ``receive``, records that the body went over, and stops it there.

    Both the flag and the exception matter. The flag is what the response
    rewrite reads, because the framework will have swallowed the exception; the
    exception is what stops the body being read any further, so an oversized
    request costs the limit rather than its own size.
    """

    def __init__(self, receive: Receive, max_bytes: int) -> None:
        self._receive = receive
        self._max_bytes = max_bytes
        self._seen = 0
        self.exceeded = False

    async def __call__(self) -> Message:
        message = await self._receive()
        if message["type"] == "http.request":
            self._seen += len(message.get("body", b""))
            if self._seen > self._max_bytes:
                self.exceeded = True
                raise _BodyTooLarge
        return message
