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

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # registers every table on Base.metadata
import tests.integration._models  # noqa: F401  (registers the sample table)
from app.core.config import AppEnv, Settings
from app.core.jwt_keys import load_key_pair
from app.db.base import Base
from app.db.session import Database, get_database, get_session
from app.main import create_app


class StubDatabase:
    """Stands in for :class:`app.db.session.Database` in unit tests."""

    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy

    async def healthcheck(self) -> bool:
        return self.healthy


# What the isolation fixture took away, kept so integration tests can ask for
# it back. Module level because both the fixture and `real_environment` need
# it, and there is exactly one test session.
_SAVED_ENV: dict[str, str] = {}
_ORIGINAL_ENV_FILE: Any = None


@pytest.fixture(autouse=True, scope="session")
def isolate_settings_from_the_environment() -> Iterator[None]:
    """Make `Settings()` in a test depend on nothing outside the repo.

    Without this, a unit test that constructs `Settings()` reads the
    developer's `.env` and any exported variable, so the suite passes or fails
    depending on whose machine it runs on. That already bit once: adding
    `LLM_PROVIDER=gemini` to a local `.env` broke two factory tests that assert
    the committed defaults, while CI stayed green.

    Tests that want a non-default value pass it explicitly. Tests that need the
    real environment, which means the ones that talk to a real database, use
    :func:`real_environment`.
    """
    global _ORIGINAL_ENV_FILE

    _ORIGINAL_ENV_FILE = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None

    # Exported variables would still win over the defaults, so they go too.
    _SAVED_ENV.update(
        {name: os.environ.pop(name) for name in list(Settings.model_fields) if name in os.environ}
    )
    try:
        yield
    finally:
        Settings.model_config["env_file"] = _ORIGINAL_ENV_FILE
        os.environ.update(_SAVED_ENV)
        _SAVED_ENV.clear()


@contextmanager
def real_environment() -> Iterator[None]:
    """Put `.env` and the exported variables back, for one block.

    An integration test depends on a real Postgres by definition, and where
    that Postgres listens is exactly what `.env` records: this machine runs it
    on 5433 because Homebrew already holds 5432. Isolating those away leaves
    every integration test trying to reach a database that is not there.
    """
    Settings.model_config["env_file"] = _ORIGINAL_ENV_FILE
    os.environ.update(_SAVED_ENV)
    try:
        yield
    finally:
        Settings.model_config["env_file"] = None
        for name in _SAVED_ENV:
            os.environ.pop(name, None)


@pytest.fixture(scope="session")
def jwt_key_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An RSA keypair for the whole session.

    Generated with the real ``scripts/gen_keys.py``, so the script is exercised
    by every run, and kept out of the repo's own ``keys/`` so a test can never
    depend on a developer having generated one.
    """
    from scripts.gen_keys import generate

    directory = tmp_path_factory.mktemp("jwt-keys")
    generate(directory, key_size=2048, force=True)
    return directory


@pytest.fixture
def settings(jwt_key_dir: Path) -> Settings:
    return Settings(
        APP_ENV=AppEnv.TEST,
        LOG_LEVEL="CRITICAL",
        JWT_PRIVATE_KEY_PATH=str(jwt_key_dir / "jwt_private.pem"),
        JWT_PUBLIC_KEY_PATH=str(jwt_key_dir / "jwt_public.pem"),
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """App whose database dependency is a healthy stub.

    The lifespan does not run under ASGITransport, so the signing keys are
    attached here the same way startup would.
    """
    application = create_app(settings)
    application.state.jwt_keys = load_key_pair(settings)
    application.dependency_overrides[get_database] = lambda: StubDatabase(healthy=True)
    return application


@pytest.fixture
def settings_only_app(settings: Settings) -> FastAPI:
    """App with no database attached, as if the lifespan never ran."""
    application = create_app(settings)
    application.state.jwt_keys = load_key_pair(settings)
    return application


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
    """Settings that deliberately do read the developer's environment."""
    with real_environment():
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


@pytest.fixture
def api_app(
    settings: Settings,
    integration_settings: Settings,
    db_session: AsyncSession,
) -> Iterator[FastAPI]:
    """An app wired to the test transaction.

    ``get_session`` is overridden to hand back the test's session, so anything
    a request writes is rolled back with the test. ``get_database`` stays real,
    because the failed-login audit deliberately opens its own transaction and
    that behaviour is worth testing rather than stubbing out.

    The application keeps the isolated settings, so its keys and limits are the
    committed defaults, but the database URL comes from the real environment:
    that audit opens a connection of its own, and it has to reach the same
    Postgres the rest of the test is using.
    """
    application = create_app(settings)
    application.state.jwt_keys = load_key_pair(settings)
    database = Database(settings, url=integration_settings.test_database_url)
    application.state.db = database

    application.dependency_overrides[get_session] = lambda: db_session
    application.dependency_overrides[get_database] = lambda: database

    yield application

    application.dependency_overrides.clear()


@pytest.fixture
async def api_client(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=api_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ------------------------------------------------------------------- browser


@pytest.fixture(scope="session")
def html_server() -> Iterator[str]:
    """Serve tests/fixtures/html on a random local port.

    Unit tests never reach the internet: every page the driver is pointed at is
    a file in this repo, served from localhost. That keeps the suite fast,
    deterministic, and runnable offline, and it means a failure is always our
    bug rather than someone else's outage.
    """
    import functools
    import http.server
    import socketserver
    import threading

    root = Path(__file__).parent / "fixtures" / "html"
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))

    class QuietServer(socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

        def handle_error(self, request: object, client_address: object) -> None:
            """Browsers drop connections routinely; that is not an error."""

    with QuietServer(("127.0.0.1", 0), handler) as httpd:
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            httpd.shutdown()
            thread.join(timeout=5)


@pytest.fixture
def browser_settings(settings: Settings) -> Settings:
    """Settings whose allow-list covers the local fixture server."""
    return settings.model_copy(
        update={
            "AGENT_HEADLESS": True,
            "AGENT_ALLOWED_DOMAINS": ["127.0.0.1", "localhost"],
            "BROWSER_ACTION_TIMEOUT_MS": 3_000,
            "BROWSER_NAVIGATION_TIMEOUT_MS": 8_000,
        }
    )


@pytest.fixture
async def driver(browser_settings: Settings) -> AsyncIterator[object]:
    """A launched driver with its own isolated context, closed afterwards."""
    from app.agent.browser.driver import PlaywrightDriver

    instance = PlaywrightDriver(browser_settings)
    await instance.launch()
    try:
        yield instance
    finally:
        await instance.close()
