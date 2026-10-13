"""Authentication endpoints (spec section 6).

The refresh token lives only in an httpOnly cookie. It is never in a response
body and never readable from JavaScript, so a cross-site scripting bug cannot
walk away with a seven day session.
"""

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse

from app.api.deps import AuthServiceDep, CurrentUser, SettingsDep, rate_limit
from app.core.config import Settings
from app.core.errors import RefreshTokenReuse, Unauthorized, error_envelope, request_id_of
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    RefreshResponse,
    RegisterRequest,
    UserResponse,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def refresh_cookie_path(settings: Settings) -> str:
    """Scope the cookie to the auth routes only.

    A cookie sent on every request is a cookie exposed on every request. These
    are the only endpoints that consume it.
    """
    return f"{settings.API_PREFIX}/auth"


def set_refresh_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        key=settings.JWT_REFRESH_COOKIE_NAME,
        value=token,
        max_age=int(settings.refresh_token_ttl.total_seconds()),
        # Unreadable from JavaScript: XSS cannot exfiltrate it.
        httponly=True,
        # Lax still sends the cookie on a same-site request. A cross-site
        # frontend needs SameSite=None with Secure; see docs/auth.md.
        samesite="lax",
        # Plain http in dev would otherwise drop the cookie entirely.
        secure=settings.is_prod,
        path=refresh_cookie_path(settings),
    )


def clear_refresh_cookie(response: Response, settings: Settings) -> None:
    """Expire the cookie. Path must match the one it was set with."""
    response.delete_cookie(
        key=settings.JWT_REFRESH_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=settings.is_prod,
        path=refresh_cookie_path(settings),
    )


def _client_context(request: Request) -> dict[str, str | None]:
    """Recorded against a refresh token so a user can recognise a session."""
    return {
        "user_agent": request.headers.get("User-Agent"),
        "ip": request.client.host if request.client else None,
    }


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    dependencies=[Depends(rate_limit("register", lambda s: s.RATE_LIMIT_REGISTER_PER_MINUTE))],
    responses={
        409: {"description": "Email already registered"},
        429: {"description": "Too many attempts"},
    },
)
async def register(payload: RegisterRequest, auth: AuthServiceDep) -> UserResponse:
    """Register a TASK_SUBMITTER.

    The response is built from an explicit schema rather than dumping the
    model, so the password hash cannot leak by accident.
    """
    user = await auth.register(
        name=payload.name,
        email=payload.email,
        password=payload.password,
    )
    return UserResponse.model_validate(user)


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Exchange credentials for an access token and a refresh cookie",
    dependencies=[Depends(rate_limit("login", lambda s: s.RATE_LIMIT_LOGIN_PER_MINUTE))],
    responses={
        401: {"description": "Email or password is incorrect"},
        429: {"description": "Too many attempts"},
    },
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    auth: AuthServiceDep,
    settings: SettingsDep,
) -> LoginResponse:
    """Authenticate, mint a 15 minute access token, and set the refresh cookie."""
    user, token = await auth.authenticate(email=payload.email, password=payload.password)
    raw_refresh = await auth.issue_refresh_token(user, **_client_context(request))
    set_refresh_cookie(response, settings, raw_refresh)

    return LoginResponse(
        access_token=token.token,
        token_type=token.token_type,
        expires_in=token.expires_in,
        user=UserResponse.model_validate(user),
    )


@router.post(
    "/refresh",
    response_model=RefreshResponse,
    summary="Rotate the refresh cookie for a new access token",
    responses={401: {"description": "Missing, invalid, expired or already-used token"}},
)
async def refresh(
    request: Request,
    auth: AuthServiceDep,
    settings: SettingsDep,
) -> Response:
    """Single-use rotation.

    The presented token is revoked and replaced. Presenting an already-revoked
    token destroys every session for that user: two parties holding one cookie
    means one of them stole it, and there is no way to tell which.

    Reuse is caught here rather than left to the global handler. The service
    revokes the chain in this request's transaction, and an exception escaping
    the route would roll that back, throwing away the revocation at exactly the
    moment it matters. Returning the 401 lets the request finish and commit.
    """
    presented = request.cookies.get(settings.JWT_REFRESH_COOKIE_NAME)
    if not presented:
        raise Unauthorized("No refresh token was supplied.")

    try:
        user, access, raw_refresh = await auth.rotate_refresh_token(
            presented, **_client_context(request)
        )
    except RefreshTokenReuse as reuse:
        rejected = JSONResponse(
            status_code=reuse.http_status,
            content=error_envelope(reuse.code, reuse.message, request_id_of(request)),
        )
        clear_refresh_cookie(rejected, settings)
        return rejected

    body = RefreshResponse(
        access_token=access.token,
        token_type=access.token_type,
        expires_in=access.expires_in,
        user=UserResponse.model_validate(user),
    )
    accepted = JSONResponse(content=body.model_dump(by_alias=True, mode="json"))
    if raw_refresh is not None:
        # None means a tolerated race: the winning request already set the cookie.
        set_refresh_cookie(accepted, settings, raw_refresh)
    return accepted


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the refresh token and clear the cookie",
)
async def logout(
    request: Request,
    response: Response,
    auth: AuthServiceDep,
    settings: SettingsDep,
) -> None:
    """Idempotent.

    Logging out is a request to end a session; a client that already lost its
    cookie has achieved that, so an unknown token is not an error.

    Returns nothing so FastAPI keeps the injected response, which is what
    carries the cookie-clearing header.
    """
    await auth.revoke_refresh_token(request.cookies.get(settings.JWT_REFRESH_COOKIE_NAME))
    clear_refresh_cookie(response, settings)


@router.get(
    "/me",
    response_model=UserResponse,
    summary="The authenticated user",
    responses={401: {"description": "Missing or invalid access token"}},
)
async def me(user: CurrentUser) -> UserResponse:
    """Read from the database, not from the token.

    The role is whatever the row says now, so a demotion applies immediately
    rather than when the access token happens to expire.
    """
    return UserResponse.model_validate(user)
