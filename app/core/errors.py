"""Error types and the handlers that render them.

Every failure leaves this service in the envelope from spec section 6::

    {"error": {"code": "...", "message": "...", "requestId": "req_..."}}

That holds for our own :class:`AppError`, for FastAPI's request validation, for
bare ``HTTPException`` raised by routing, and for anything unhandled. A stack
trace is logged but never returned.
"""

from typing import Any, ClassVar, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from app.core.logging import get_logger

__all__ = [
    "AppError",
    "Conflict",
    "DependencyUnavailable",
    "Forbidden",
    "InsufficientCredits",
    "InvalidCredentials",
    "NotFound",
    "RateLimited",
    "RefreshTokenReuse",
    "Unauthorized",
    "UnprocessableAction",
    "ValidationFailed",
    "register_exception_handlers",
]

logger = get_logger(__name__)

UNKNOWN_REQUEST_ID: Final = "req_unknown"


class AppError(Exception):
    """Base class for every expected failure.

    Subclasses set ``code`` and ``http_status``; the message is per-instance so
    a caller can be specific without inventing a new class.
    """

    code: ClassVar[str] = "INTERNAL_ERROR"
    http_status: ClassVar[int] = 500
    default_message: ClassVar[str] = "Something went wrong."

    def __init__(
        self,
        message: str | None = None,
        *,
        headers: dict[str, str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.default_message
        # Some failures carry a header the client is meant to act on, such as
        # Retry-After on a 429.
        self.headers = headers or {}
        # Numbers a client can act on, when the message alone would send
        # somebody to another screen to find them: how short a wallet is, for
        # instance. Never anything sensitive; this is rendered to the caller.
        self.details = details or {}
        super().__init__(self.message)


class ValidationFailed(AppError):
    code = "VALIDATION_FAILED"
    http_status = 400
    default_message = "The request payload is invalid."


class Unauthorized(AppError):
    code = "UNAUTHORIZED"
    http_status = 401
    default_message = "Authentication is required."


class InvalidCredentials(Unauthorized):
    """Wrong password, or no such account.

    Deliberately one error for both: spec section 10's spirit is that an
    unauthenticated caller learns nothing, and distinguishing the two would turn
    the login form into an account-existence oracle.
    """

    code = "INVALID_CREDENTIALS"
    default_message = "Email or password is incorrect."


class RefreshTokenReuse(Unauthorized):
    """A revoked refresh token was presented again.

    Distinct from plain Unauthorized so the route can finish normally and let
    the chain revocation commit, instead of letting the 401 roll it back.
    """

    code = "UNAUTHORIZED"
    default_message = "The refresh token is invalid."


class InsufficientCredits(AppError):
    code = "INSUFFICIENT_CREDITS"
    http_status = 402
    default_message = "Not enough credits to start this task."


class Forbidden(AppError):
    code = "FORBIDDEN"
    http_status = 403
    default_message = "You are not allowed to do that."


class NotFound(AppError):
    code = "NOT_FOUND"
    http_status = 404
    default_message = "The requested resource does not exist."


class Conflict(AppError):
    code = "CONFLICT"
    http_status = 409
    default_message = "That conflicts with the current state."


class UnprocessableAction(AppError):
    code = "UNPROCESSABLE_ACTION"
    http_status = 422
    default_message = "The action cannot be carried out."


class RateLimited(AppError):
    code = "RATE_LIMITED"
    http_status = 429
    default_message = "Too many requests. Try again shortly."


class DependencyUnavailable(AppError):
    code = "DEPENDENCY_UNAVAILABLE"
    http_status = 503
    default_message = "A dependency is unavailable."


# Bare HTTPException (routing 404s, 405s) still has to leave in the envelope.
_STATUS_TO_CODE: Final[dict[int, str]] = {
    400: ValidationFailed.code,
    401: Unauthorized.code,
    402: InsufficientCredits.code,
    403: Forbidden.code,
    404: NotFound.code,
    405: "METHOD_NOT_ALLOWED",
    409: Conflict.code,
    422: UnprocessableAction.code,
    429: RateLimited.code,
    503: DependencyUnavailable.code,
}


def request_id_of(request: Request) -> str:
    """The id the middleware bound, or a placeholder if it never ran."""
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else UNKNOWN_REQUEST_ID


def error_envelope(
    code: str,
    message: str,
    request_id: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message, "requestId": request_id}
    if details:
        body["details"] = details
    return {"error": body}


def _respond(
    request: Request,
    status: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=error_envelope(code, message, request_id_of(request), details),
        headers=headers or None,
    )


async def app_error_handler(request: Request, exc: Exception) -> Response:
    error = exc if isinstance(exc, AppError) else AppError()
    log = logger.warning if error.http_status < 500 else logger.error
    log(
        "request_failed",
        code=error.code,
        status=error.http_status,
        path=request.url.path,
    )
    return _respond(
        request,
        error.http_status,
        error.code,
        error.message,
        error.headers,
        error.details,
    )


async def validation_error_handler(request: Request, exc: Exception) -> Response:
    """FastAPI request validation.

    Mapped to 400, not FastAPI's default 422: spec section 6 assigns 400 to
    validation and reserves 422 for an action the agent cannot carry out.
    """
    detail = ""
    if isinstance(exc, RequestValidationError):
        errors = exc.errors()
        if errors:
            first = errors[0]
            location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
            detail = f"{location}: {first.get('msg', '')}".strip(": ")
    message = detail or ValidationFailed.default_message
    logger.warning("request_validation_failed", path=request.url.path, detail=detail)
    return _respond(request, ValidationFailed.http_status, ValidationFailed.code, message)


async def http_exception_handler(request: Request, exc: Exception) -> Response:
    status = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
    detail = getattr(exc, "detail", None)
    code = _STATUS_TO_CODE.get(status, "HTTP_ERROR")
    message = str(detail) if detail else "Request failed."
    return _respond(request, status, code, message)


async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    """Last resort. The trace goes to the log, never to the client."""
    logger.exception(
        "unhandled_exception",
        path=request.url.path,
        error_type=type(exc).__name__,
    )
    return _respond(request, 500, AppError.code, AppError.default_message)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
