"""Startup must clean up partial resources, and HTTP success must follow commit."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from httpx import ASGITransport, AsyncClient

import app.main as main
from app.core.config import Settings
from app.db.session import SessionDep, get_database
from app.main import create_app
from app.services.task_runner import TaskRunner


@pytest.mark.parametrize("failure", ["database", "runner", "outbox", "none"])
async def test_startup_cleanup(monkeypatch: pytest.MonkeyPatch, settings: Settings, failure: str):
    database = SimpleNamespace(dispose=AsyncMock(), healthcheck=AsyncMock(return_value=True))
    runner = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    outbox = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    billing = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(main, "Database", Mock(return_value=database))
    monkeypatch.setattr(main, "TaskRunner", Mock(return_value=runner))
    monkeypatch.setattr(main, "OutboxWorker", Mock(return_value=outbox))
    monkeypatch.setattr(main, "BillingClient", Mock(return_value=billing))
    if failure == "database":
        database.healthcheck.return_value = False
    elif failure in ("runner", "outbox"):
        (runner if failure == "runner" else outbox).start.side_effect = RuntimeError(
            "startup failed"
        )
    application = create_app(settings)
    if failure == "none":
        async with application.router.lifespan_context(application):
            runner.start.assert_awaited_once()
    else:
        with pytest.raises(RuntimeError):
            async with application.router.lifespan_context(application):
                pytest.fail("Failed startup must not serve requests")
    runner.stop.assert_awaited_once()
    outbox.stop.assert_awaited_once()
    billing.aclose.assert_awaited_once()
    database.dispose.assert_awaited_once()


async def test_partial_browser_launch_is_cleaned_up(settings: Settings):
    runner = TaskRunner(settings, database=None)
    playwright = SimpleNamespace(stop=AsyncMock())
    runner._playwright = playwright
    # Browser launch failed after Playwright started, before _started was set.
    await runner.stop()
    playwright.stop.assert_awaited_once()
    assert runner._playwright is None


async def test_failed_commit_does_not_send_success(settings: Settings):
    @asynccontextmanager
    async def session():
        yield object()
        raise RuntimeError("commit failed")

    application = create_app(settings)
    application.dependency_overrides[get_database] = lambda: SimpleNamespace(session=session)

    @application.post("/commit-probe", status_code=201)
    async def probe(session: SessionDep):
        return {"created": True}

    transport = ASGITransport(app=application, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/commit-probe")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
