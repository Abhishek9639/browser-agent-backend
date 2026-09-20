"""Shared FastAPI dependencies.

Settings, keys and the database come from ``request.app.state``, not from module
level, so an app built by ``create_app(settings)`` really is configured by those
settings all the way down to the handlers. Tests depend on this.
"""

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings
from app.core.errors import Unauthorized
from app.core.jwt_keys import KeyPair
from app.core.security import TokenClaims, TokenService
from app.db.session import DatabaseDep
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
) -> AuthService:
    return AuthService(uow, tokens, database)


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
