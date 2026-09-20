"""B02: the health endpoint."""

from httpx import AsyncClient


async def test_health_returns_200(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == "0.1.0"
    assert body["env"] == "test"
    assert body["time"]


async def test_health_lives_under_the_api_prefix(client: AsyncClient) -> None:
    """Decision A9: health is versioned, unlike /metrics."""
    assert (await client.get("/health")).status_code == 404
