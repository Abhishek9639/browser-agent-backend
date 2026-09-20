"""Application factory.

``create_app`` builds a fully wired FastAPI instance. Everything it needs comes
from :func:`app.core.config.get_settings`, so tests can override settings and
build their own app without touching module-level state.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestContextMiddleware

__all__ = ["app", "create_app"]

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown.

    The database pool, the Playwright browser and the task runner attach here in
    later parts; each one starts up in order and is torn down in reverse.
    """
    settings: Settings = application.state.settings
    logger.info(
        "startup",
        app=settings.APP_NAME,
        version=settings.APP_VERSION,
        env=settings.APP_ENV.value,
    )

    # B05: await db.connect()
    # B19: await browser_pool.start()
    # B30: await task_runner.start()

    try:
        yield
    finally:
        # B30: await task_runner.stop()
        # B19: await browser_pool.stop()
        # B05: await db.disconnect()
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

    # Outermost first: the request id must exist before anything else runs.
    application.add_middleware(RequestContextMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,  # the refresh cookie is httpOnly and cross-origin
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    register_exception_handlers(application)
    application.include_router(api_router, prefix=settings.API_PREFIX)

    return application


app = create_app()
