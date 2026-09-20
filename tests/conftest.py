"""Shared fixtures.

Starlette 1.6 deprecates TestClient-on-httpx, so the suite drives the ASGI app
directly with ``httpx.AsyncClient``. That is async end to end, which is what
CLAUDE.md asks for anyway.
"""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import AppEnv, Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings(APP_ENV=AppEnv.TEST, LOG_LEVEL="CRITICAL")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
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
