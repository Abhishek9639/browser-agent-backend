"""Application factory.

``create_app`` builds a fully wired FastAPI instance. Everything it needs comes
from :func:`app.core.config.get_settings`, so tests can override settings and
build their own app without touching module-level state.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.orchestrator import database_unit_of_work
from app.api.ops import router as ops_router
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.jwt_keys import load_key_pair
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestContextMiddleware
from app.core.rate_limit import InMemoryRateLimiter
from app.core.security_headers import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from app.db.session import Database
from app.services.approvals import ApprovalRegistry
from app.services.billing import OutboxWorker
from app.services.billing_client import BillingClient
from app.services.events import InMemoryEventBus
from app.services.storage import LocalFileStorage
from app.services.task_runner import TaskRunner

__all__ = ["app", "create_app"]

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown.

    Each dependency starts up in order and is torn down in reverse. The task
    runner is last up and first down, because it is the only one that holds
    work in flight: stopping it first gives running tasks a chance to write
    their own terminal status while the database is still there to write to.
    """
    settings: Settings = application.state.settings
    logger.info(
        "startup",
        app=settings.APP_NAME,
        version=settings.APP_VERSION,
        env=settings.APP_ENV.value,
    )

    database = Database(settings)
    application.state.db = database
    logger.info("database_configured", database=settings.POSTGRES_DB)

    # Loaded once at startup, and loudly: a service that boots without its
    # signing key would accept registrations and then fail every login.
    application.state.jwt_keys = load_key_pair(settings)
    logger.info("signing_keys_loaded", kid=settings.JWT_KID)

    # Per process. See the note in app/core/rate_limit.py before scaling out.
    application.state.rate_limiter = InMemoryRateLimiter()

    # One bus per process. A client connected to another worker will not see
    # these events; see the note in app/services/events.py.
    application.state.event_bus = InMemoryEventBus()
    application.state.screenshot_storage = LocalFileStorage(settings)
    # Shared between the runner, which waits on approvals, and the API, which
    # answers them. Both have to be looking at the same one.
    application.state.approval_registry = ApprovalRegistry()

    runner = TaskRunner(
        settings,
        database,
        events=application.state.event_bus,
        storage=application.state.screenshot_storage,
        approvals=application.state.approval_registry,
    )
    application.state.task_runner = runner

    # Delivers the billing outbox. Started even when the backlog is empty,
    # because a backlog left by the last process is the case it exists for; it
    # does nothing at all when billing is disabled.
    billing_client = BillingClient(settings)
    application.state.billing_client = billing_client
    outbox = OutboxWorker(database_unit_of_work(database), settings, billing_client)
    application.state.billing_outbox_worker = outbox

    # Chromium takes a second or so to launch, and a failure here should stop
    # the process rather than surface as every task failing one by one.
    await runner.start()
    await outbox.start()

    try:
        yield
    finally:
        # Torn down in reverse order of startup. The outbox goes before the
        # database, because it needs the database to record what it delivered,
        # and after the runner, so a task finishing during shutdown still gets
        # its settle queued.
        await runner.stop()
        await outbox.stop()
        await billing_client.aclose()
        await database.dispose()
        logger.info("shutdown", app=settings.APP_NAME)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Pass ``settings`` to override configuration."""
    settings = settings or get_settings()
    configure_logging(settings)

    application = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        docs_url="/docs" if not settings.is_prod else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_prod else None,
        lifespan=lifespan,
    )
    application.state.settings = settings

    # Starlette applies middleware in reverse registration order, so the last
    # one added is the outermost. The order below is deliberate:
    #
    #   security headers -> body limit -> CORS -> request context -> routes
    #
    # Headers outermost so even a response this stack rejects carries them. The
    # body limit above CORS so an oversized preflight-less POST is refused
    # before anything reads it. The request context innermost of the four, so
    # the access log and the API metrics measure the handler rather than the
    # middleware around it.
    application.add_middleware(RequestContextMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,  # the refresh cookie is httpOnly and cross-origin
        # Named rather than "*". With credentials allowed, a wildcard is both
        # refused by browsers and a statement that any header and any method is
        # welcome, which is the opposite of what section 10 asks for.
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )
    application.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.MAX_REQUEST_BYTES)
    application.add_middleware(
        SecurityHeadersMiddleware,
        is_prod=settings.is_prod,
        hsts_max_age=settings.HSTS_MAX_AGE_SECONDS,
    )

    register_exception_handlers(application)
    # JWKS lives at the root per RFC 8615, everything else is versioned.
    application.include_router(ops_router)
    application.include_router(api_router, prefix=settings.API_PREFIX)

    return application


app = create_app()
