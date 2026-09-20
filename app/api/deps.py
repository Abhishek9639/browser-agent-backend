"""Shared FastAPI dependencies.

Settings, keys and the database come from ``request.app.state``, not from module
level, so an app built by ``create_app(settings)`` really is configured by those
settings all the way down to the handlers. Tests depend on this.
"""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings
from app.core.errors import Forbidden, RateLimited, Unauthorized
from app.core.jwt_keys import KeyPair
from app.core.rate_limit import InMemoryRateLimiter, RateLimiter
from app.core.security import TokenClaims, TokenService
from app.db.session import DatabaseDep
from app.domain.enums import UserRole
from app.models.user import User
from app.repositories.unit_of_work import UnitOfWorkDep
from app.services.auth import AuthService

__all__ = [
    "AuthServiceDep",
    "CurrentUser",
    "KeyPairDep",
    "SettingsDep",
    "TokenServiceDep",
    "get_app_settings",
    "get_current_user",
    "get_key_pair",
    "get_token_service",
]

# auto_error=False so a missing header reaches our handler and leaves in the
# spec's error envelope, rather than FastAPI's own 403 body.
_bearer = HTTPBearer(auto_error=False, description="Access token from POST /auth/login")


def get_app_settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if isinstance(settings, Settings):
        return settings
    return get_settings()


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def get_key_pair(request: Request) -> KeyPair:
    keys = getattr(request.app.state, "jwt_keys", None)
    if not isinstance(keys, KeyPair):  # pragma: no cover - startup guarantees this
        raise Unauthorized("Signing keys are not configured.")
    return keys


KeyPairDep = Annotated[KeyPair, Depends(get_key_pair)]


def get_token_service(settings: SettingsDep, keys: KeyPairDep) -> TokenService:
    return TokenService(settings, keys)


TokenServiceDep = Annotated[TokenService, Depends(get_token_service)]


def get_auth_service(
    uow: UnitOfWorkDep,
    tokens: TokenServiceDep,
    database: DatabaseDep,
    settings: SettingsDep,
) -> AuthService:
    return AuthService(uow, tokens, database, settings)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


async def get_token_claims(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    tokens: TokenServiceDep,
) -> TokenClaims:
    """Validate the bearer token, or 401."""
    if credentials is None or not credentials.credentials.strip():
        raise Unauthorized("An access token is required.")
    if credentials.scheme.lower() != "bearer":
        raise Unauthorized("Authorization must use the Bearer scheme.")
    return tokens.decode_access_token(credentials.credentials)


async def get_current_user(
    claims: Annotated[TokenClaims, Depends(get_token_claims)],
    uow: UnitOfWorkDep,
) -> User:
    """The authenticated caller.

    The token is re-checked against the database rather than trusted on its
    own, so an account deleted mid-session stops working immediately instead of
    lasting until the token expires. The role comes from the row, not the
    token, for the same reason: a demotion takes effect at once.
    """
    user = await uow.users.get(claims.subject)
    if user is None:
        raise Unauthorized("The access token is invalid.")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def client_ip(request: Request, settings: Settings) -> str:
    """The address a rate-limit bucket is keyed on.

    ``X-Forwarded-For`` is honoured only when explicitly enabled, because any
    client can send it. Trusting it by default would let one caller mint a
    fresh bucket per request and defeat the limit entirely.
    """
    if settings.RATE_LIMIT_TRUST_FORWARDED_FOR:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            # Left-most entry is the original client.
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def get_rate_limiter(request: Request) -> RateLimiter:
    limiter = getattr(request.app.state, "rate_limiter", None)
    if not isinstance(limiter, RateLimiter):  # pragma: no cover - startup sets it
        limiter = InMemoryRateLimiter()
        request.app.state.rate_limiter = limiter
    return limiter


RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]


def rate_limit(
    name: str,
    limit_of: Callable[[Settings], int],
    *,
    window_seconds: int = 60,
) -> Callable[..., Coroutine[Any, Any, None]]:
    """Build a dependency that throttles a route by client address.

    ``limit_of`` reads the quota from settings at request time rather than at
    import time, so a test can build an app with a different limit without
    re-importing the router.

    Keyed on route name plus address, so exhausting the login quota does not
    also block registration.
    """

    async def dependency(
        request: Request,
        limiter: RateLimiterDep,
        settings: SettingsDep,
    ) -> None:
        if not settings.RATE_LIMIT_ENABLED:
            return

        key = f"{name}:{client_ip(request, settings)}"
        result = await limiter.acquire(key, limit=limit_of(settings), window_seconds=window_seconds)
        if not result.allowed:
            raise RateLimited(headers={"Retry-After": str(result.retry_after)})

    return dependency


def require_roles(*roles: UserRole) -> Callable[..., Coroutine[Any, Any, User]]:
    """Allow only the listed roles.

    401 means "we do not know who you are"; 403 means "we do, and you may not".
    Conflating them would tell an unauthenticated caller that the resource
    exists and that some role can reach it.

    ADMIN is not implied. Every call site lists the roles it accepts, so
    reading a route tells you exactly who can reach it.
    """
    allowed = frozenset(roles)

    async def dependency(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise Forbidden("Your role does not allow this action.")
        return user

    return dependency
