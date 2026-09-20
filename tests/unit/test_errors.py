"""B02: every failure leaves in the spec section 6 envelope."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.core.config import Settings
from app.core.errors import Conflict, UnprocessableAction
from app.main import create_app

LEAKY_DETAIL = "connection string postgres://user:pw@host"


class Payload(BaseModel):
    instruction: str
    max_steps: int


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """App with routes that fail in each of the ways we care about."""
    application = create_app(settings)

    @application.get("/api/v1/boom/app-error")
    async def _app_error() -> None:
        raise Conflict("that task is already running")

    @application.get("/api/v1/boom/unprocessable")
    async def _unprocessable() -> None:
        raise UnprocessableAction()

    @application.get("/api/v1/boom/unhandled")
    async def _unhandled() -> None:
        raise RuntimeError(LEAKY_DETAIL)

    @application.post("/api/v1/boom/validate")
    async def _validate(payload: Payload) -> Payload:
        return payload

    return application


def assert_envelope(body: dict[str, object]) -> dict[str, str]:
    """The response body is exactly {"error": {code, message, requestId}}."""
    assert set(body) == {"error"}
    error = body["error"]
    assert isinstance(error, dict)
    assert set(error) == {"code", "message", "requestId"}
    assert all(isinstance(value, str) and value for value in error.values())
    assert error["requestId"].startswith("req_")
    return error  # type: ignore[return-value]


async def test_custom_app_error(client: AsyncClient) -> None:
    response = await client.get("/api/v1/boom/app-error")

    assert response.status_code == 409
    error = assert_envelope(response.json())
    assert error["code"] == "CONFLICT"
    assert error["message"] == "that task is already running"


async def test_unprocessable_action_is_422(client: AsyncClient) -> None:
    response = await client.get("/api/v1/boom/unprocessable")

    assert response.status_code == 422
    assert assert_envelope(response.json())["code"] == "UNPROCESSABLE_ACTION"


async def test_unknown_route_is_404(client: AsyncClient) -> None:
    response = await client.get("/api/v1/definitely-not-a-route")

    assert response.status_code == 404
    assert assert_envelope(response.json())["code"] == "NOT_FOUND"


async def test_request_validation_is_400_not_422(client: AsyncClient) -> None:
    """Spec section 6 gives 400 to validation and reserves 422 for actions."""
    response = await client.post("/api/v1/boom/validate", json={"instruction": "go"})

    assert response.status_code == 400
    error = assert_envelope(response.json())
    assert error["code"] == "VALIDATION_FAILED"
    assert "max_steps" in error["message"]


async def test_unhandled_exception_is_500_and_leaks_nothing(client: AsyncClient) -> None:
    response = await client.get("/api/v1/boom/unhandled")

    assert response.status_code == 500
    error = assert_envelope(response.json())
    assert error["code"] == "INTERNAL_ERROR"
    assert LEAKY_DETAIL not in response.text
    assert "Traceback" not in response.text
    assert "RuntimeError" not in response.text


async def test_every_error_carries_the_caller_request_id(app: FastAPI) -> None:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for path in (
            "/api/v1/boom/app-error",
            "/api/v1/boom/unprocessable",
            "/api/v1/boom/unhandled",
            "/api/v1/nope",
        ):
            response = await client.get(path, headers={"X-Request-ID": "req_traced"})
            assert response.json()["error"]["requestId"] == "req_traced", path
