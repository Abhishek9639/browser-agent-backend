"""Shared FastAPI dependencies.

Settings, keys and the database come from ``request.app.state``, not from module
level, so an app built by ``create_app(settings)`` really is configured by those
settings all the way down to the handlers. Tests depend on this.
"""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.requests import HTTPConnection

from app.agent.orchestrator import database_unit_of_work
from app.core.config import Settings, get_settings
from app.core.errors import DependencyUnavailable, Forbidden, RateLimited, Unauthorized
from app.core.jwt_keys import KeyPair
from app.core.rate_limit import InMemoryRateLimiter, RateLimiter
from app.core.security import TokenClaims, TokenService
from app.db.session import DatabaseDep
from app.domain.enums import UserRole
from app.models.user import User
from app.repositories.unit_of_work import UnitOfWorkDep
from app.services.approvals import ApprovalRegistry, ApprovalService
from app.services.auth import AuthService
from app.services.billing import BillingService
from app.services.billing_client import BillingClient
from app.services.events import EventBus, InMemoryEventBus
from app.services.stats import StatsService
from app.services.storage import LocalFileStorage, ScreenshotStorage
from app.services.stream import StreamService
from app.services.task_runner import TaskRunner
from app.services.task_service import TaskService
from app.services.ws_tickets import InMemoryTicketStore, TicketStore

__all__ = [
    "ApprovalRegistryDep",
    "ApprovalServiceDep",
    "AuthServiceDep",
    "BillingClientDep",
    "BillingServiceDep",
    "CurrentUser",
    "EventBusDep",
    "KeyPairDep",
    "ScreenshotStorageDep",
    "SettingsDep",
    "StatsServiceDep",
    "StreamServiceDep",
    "TaskRunnerDep",
    "TaskServiceDep",
    "TicketStoreDep",
    "TokenServiceDep",
    "get_app_settings",
    "get_approval_registry",
    "get_approval_service",
    "get_billing_client",
    "get_billing_service",
    "get_current_user",
    "get_event_bus",
    "get_key_pair",
    "get_screenshot_storage",
    "get_stats_service",
    "get_stream_service",
    "get_task_runner",
    "get_task_service",
    "get_ticket_store",
    "get_token_service",
]

# auto_error=False so a missing header reaches our handler and leaves in the
# spec's error envelope, rather than FastAPI's own 403 body.
_bearer = HTTPBearer(auto_error=False, description="Access token from POST /auth/login")


def get_app_settings(connection: HTTPConnection) -> Settings:
    """Settings for this app.

    Typed as ``HTTPConnection`` rather than ``Request`` because it is also
    resolved for WebSocket routes, and a websocket is not a request. FastAPI
    cannot inject a ``Request`` there at all, so anything a socket depends on
    has to take the common base class.
    """
    settings = getattr(connection.app.state, "settings", None)
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


def get_screenshot_storage(connection: HTTPConnection, settings: SettingsDep) -> ScreenshotStorage:
    """The app's storage backend, built once at startup.

    Falls back to a local one rather than failing, so a unit test that builds a
    bare app can still reach a route that declares this dependency.
    """
    storage = getattr(connection.app.state, "screenshot_storage", None)
    if isinstance(storage, ScreenshotStorage):
        return storage
    return LocalFileStorage(settings)


ScreenshotStorageDep = Annotated[ScreenshotStorage, Depends(get_screenshot_storage)]


def get_billing_client(connection: HTTPConnection, settings: SettingsDep) -> BillingClient:
    """One client per process, held on app state.

    One because it owns a connection pool, and pointing at the payments service
    from a fresh client per request would open a connection per request. Built
    lazily so a test app that never ran a lifespan still gets one.
    """
    client = getattr(connection.app.state, "billing_client", None)
    if isinstance(client, BillingClient):
        return client
    client = BillingClient(settings)
    connection.app.state.billing_client = client
    return client


BillingClientDep = Annotated[BillingClient, Depends(get_billing_client)]


def get_billing_service(
    uow: UnitOfWorkDep,
    settings: SettingsDep,
    client: BillingClientDep,
) -> BillingService:
    return BillingService(uow, settings, client=client)


BillingServiceDep = Annotated[BillingService, Depends(get_billing_service)]


def get_task_service(
    uow: UnitOfWorkDep,
    settings: SettingsDep,
    storage: ScreenshotStorageDep,
    billing: BillingServiceDep,
) -> TaskService:
    return TaskService(uow, settings, storage, billing=billing)


TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]


def get_stats_service(uow: UnitOfWorkDep, settings: SettingsDep) -> StatsService:
    return StatsService(uow, settings)


StatsServiceDep = Annotated[StatsService, Depends(get_stats_service)]


def get_approval_registry(connection: HTTPConnection) -> ApprovalRegistry:
    """The approvals this process is waiting on.

    On app state so the HTTP request that answers one can wake the run that
    is blocked on it, which is only possible when both are in this process.
    """
    registry = getattr(connection.app.state, "approval_registry", None)
    if isinstance(registry, ApprovalRegistry):
        return registry
    registry = ApprovalRegistry()
    connection.app.state.approval_registry = registry
    return registry


ApprovalRegistryDep = Annotated[ApprovalRegistry, Depends(get_approval_registry)]


def get_approval_service(
    uow: UnitOfWorkDep,
    registry: ApprovalRegistryDep,
    bus: "EventBusDep",
    settings: SettingsDep,
) -> ApprovalService:
    return ApprovalService(
        uow,
        registry,
        bus,
        owner_can_approve=settings.APPROVAL_OWNER_CAN_APPROVE,
    )


ApprovalServiceDep = Annotated[ApprovalService, Depends(get_approval_service)]


def get_event_bus(connection: HTTPConnection) -> EventBus:
    """The bus the lifespan created, or a fresh one for a bare test app."""
    bus = getattr(connection.app.state, "event_bus", None)
    if isinstance(bus, EventBus):
        return bus
    bus = InMemoryEventBus()
    connection.app.state.event_bus = bus
    return bus


EventBusDep = Annotated[EventBus, Depends(get_event_bus)]


def get_ticket_store(connection: HTTPConnection, settings: SettingsDep) -> TicketStore:
    """One store per process, held on app state.

    Built lazily rather than in the lifespan so a test app that never started
    one can still issue a ticket.
    """
    store = getattr(connection.app.state, "ticket_store", None)
    if isinstance(store, TicketStore):
        return store
    store = InMemoryTicketStore(settings.WS_TICKET_TTL_SECONDS)
    connection.app.state.ticket_store = store
    return store


TicketStoreDep = Annotated[TicketStore, Depends(get_ticket_store)]


def get_stream_service(
    database: DatabaseDep,
    storage: ScreenshotStorageDep,
) -> StreamService:
    """Opens its own short sessions.

    Deliberately not the request session: a WebSocket lives for the whole run
    of a task, and holding a database connection open for that long would tie
    up the pool for nothing.
    """
    return StreamService(database_unit_of_work(database), storage)


StreamServiceDep = Annotated[StreamService, Depends(get_stream_service)]


def get_task_runner(request: Request) -> TaskRunner:
    """The runner the lifespan started.

    A 503 rather than a silent no-op when it is missing: a task accepted by an
    API whose runner never started would sit in SUBMITTED forever, and the
    client would have no way to tell that from a queue that is merely busy.
    """
    runner = getattr(request.app.state, "task_runner", None)
    if not isinstance(runner, TaskRunner):
        raise DependencyUnavailable("The task runner is not available.")
    return runner


TaskRunnerDep = Annotated[TaskRunner, Depends(get_task_runner)]


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
