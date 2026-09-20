"""B02/B03: the health endpoint."""

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.db.session import get_database
from tests.conftest import StubDatabase


async def test_health_returns_200_when_the_database_is_up(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["version"] == "0.1.0"
    assert body["env"] == "test"
    assert body["time"]


async def test_health_returns_503_when_the_database_is_down(app: FastAPI) -> None:
    """A process that cannot reach Postgres cannot serve a task."""
    app.dependency_overrides[get_database] = lambda: StubDatabase(healthy=False)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["db"] == "down"
    # Still the health payload, not the error envelope: a probe wants detail.
    assert "error" not in body


async def test_health_reports_503_when_no_database_is_configured(
    settings_only_app: FastAPI,
) -> None:
    """No lifespan, no db on app.state: the dependency raises 503 in the envelope."""
    transport = ASGITransport(app=settings_only_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


async def test_health_lives_under_the_api_prefix(client: AsyncClient) -> None:
    """Decision A9: health is versioned, unlike /metrics."""
    assert (await client.get("/health")).status_code == 404
