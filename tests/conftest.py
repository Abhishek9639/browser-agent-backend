"""Shared fixtures.

Two worlds live here:

* Unit tests never touch Postgres. The ``get_database`` dependency is overridden
  with a stub, so ``make check`` stays fast and runs anywhere.
* Integration tests get a real engine against ``agent_core_test``, with the
  schema created once per session and every test wrapped in a transaction that
  is rolled back.

Starlette 1.6 deprecates TestClient-on-httpx, so the suite drives the ASGI app
directly with ``httpx.AsyncClient``.
"""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import tests.integration._models  # noqa: F401  (registers the sample table)
from app.core.config import AppEnv, Settings
from app.db.base import Base
from app.db.session import get_database
from app.main import create_app


class StubDatabase:
    """Stands in for :class:`app.db.session.Database` in unit tests."""

    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy

    async def healthcheck(self) -> bool:
        return self.healthy


@pytest.fixture
def settings() -> Settings:
    return Settings(APP_ENV=AppEnv.TEST, LOG_LEVEL="CRITICAL")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """App whose database dependency is a healthy stub."""
    application = create_app(settings)
    application.dependency_overrides[get_database] = lambda: StubDatabase(healthy=True)
    return application


@pytest.fixture
def settings_only_app(settings: Settings) -> FastAPI:
    """App with no database attached, as if the lifespan never ran."""
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Client that returns the 500 response instead of re-raising.

    ``raise_app_exceptions=False`` is what lets us assert on the envelope the
    unhandled-exception handler produces.
    """
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


# --------------------------------------------------------------- integration


@pytest.fixture(scope="session")
def integration_settings() -> Settings:
    return Settings(APP_ENV=AppEnv.TEST, LOG_LEVEL="CRITICAL")


@pytest.fixture(scope="session")
async def db_engine(integration_settings: Settings) -> AsyncIterator[AsyncEngine]:
    """Engine against agent_core_test, schema built once and torn down after.

    NullPool keeps connections from outliving the session-scoped event loop.
    """
    engine = create_async_engine(integration_settings.test_database_url, poolclass=NullPool)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session inside a transaction that is always rolled back.

    The session may commit freely: ``join_transaction_mode="create_savepoint"``
    turns its commits into savepoint releases inside the outer transaction, so
    tests stay isolated without truncating tables between them.
    """
    connection = await db_engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()
