"""Response headers and the body size limit (spec section 10).

Both are middleware, so both are tested through a real app: a unit test of the
header dictionary would not catch the thing most likely to go wrong, which is
the middleware sitting in the wrong place in the stack.
"""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import AppEnv, Settings
from app.core.security_headers import API_CSP, DOCS_CSP, is_docs_path
from app.main import create_app


def app_for(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app_for(settings))
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


# ----------------------------------------------------------------- headers


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Cross-Origin-Resource-Policy", "same-origin"),
        ("Content-Security-Policy", API_CSP),
    ],
)
async def test_every_response_carries_the_header(
    client: AsyncClient, header: str, value: str
) -> None:
    response = await client.get("/api/v1/health")

    assert response.headers[header] == value


async def test_the_headers_are_set_on_an_error_too(client: AsyncClient) -> None:
    """The outermost middleware, so a rejected request is covered as well.

    An error body is the one most likely to be sniffed as HTML, because it is
    the one a browser is most likely to be looking at directly.
    """
    response = await client.get("/api/v1/tasks")

    assert response.status_code == 401
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Security-Policy"] == API_CSP


async def test_the_api_policy_denies_everything(client: AsyncClient) -> None:
    """An API answers with JSON: nothing should load, frame or submit to it."""
    policy = (await client.get("/api/v1/health")).headers["Content-Security-Policy"]

    assert "default-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "base-uri 'none'" in policy
    assert "form-action 'none'" in policy
    assert "unsafe-inline" not in policy


async def test_the_docs_get_a_policy_that_lets_them_work(client: AsyncClient) -> None:
    """Swagger UI loads a script from a CDN, so the API policy would break it.

    Loosened only for the documentation routes, which are not registered in
    production at all.
    """
    response = await client.get("/docs")

    assert response.status_code == 200
    assert response.headers["Content-Security-Policy"] == DOCS_CSP


def test_the_docs_policy_is_matched_exactly_not_by_prefix() -> None:
    """``/docsomething`` must not pick up the policy that allows a CDN script."""
    assert is_docs_path("/docs")
    assert is_docs_path("/openapi.json")
    assert not is_docs_path("/docsomething")
    assert not is_docs_path("/api/v1/docs")


async def test_hsts_is_sent_in_production_only(settings: Settings) -> None:
    """A dev server on plain HTTP teaching a browser to refuse localhost would
    last two years, which is a long time to regret one header."""
    transport = ASGITransport(app=app_for(settings))
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        dev = await http.get("/api/v1/health")

    production = settings.model_copy(update={"APP_ENV": AppEnv.PROD})
    transport = ASGITransport(app=app_for(production))
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        prod = await http.get("/api/v1/health")

    assert "Strict-Transport-Security" not in dev.headers
    assert prod.headers["Strict-Transport-Security"].startswith("max-age=")
    assert "includeSubDomains" in prod.headers["Strict-Transport-Security"]


# --------------------------------------------------------------- body limit


async def test_a_body_larger_than_the_limit_is_refused(settings: Settings) -> None:
    small = settings.model_copy(update={"MAX_REQUEST_BYTES": 512})
    transport = ASGITransport(app=app_for(small))

    async with AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post(
            "/api/v1/auth/login",
            content=b"x" * 2048,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


async def test_a_body_within_the_limit_still_reaches_the_handler(settings: Settings) -> None:
    transport = ASGITransport(app=app_for(settings))

    async with AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post(
            "/api/v1/auth/login", json={"email": "nobody@example.test", "password": "x" * 20}
        )

    # 401 or 400, never 413: the point is that the body was read and validated.
    assert response.status_code != 413


async def test_a_lying_content_length_is_still_caught(settings: Settings) -> None:
    """The header is a claim.

    A chunked request has no length at all, and a dishonest one can say
    whatever it likes, so the body is counted as it arrives as well.
    """
    small = settings.model_copy(update={"MAX_REQUEST_BYTES": 512})
    transport = ASGITransport(app=app_for(small))

    async def oversized() -> AsyncIterator[bytes]:
        for _ in range(8):
            yield b"x" * 256

    async with AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post(
            "/api/v1/auth/login",
            content=oversized(),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413


# --------------------------------------------------------------------- CORS


async def test_cors_allows_the_configured_origin_with_credentials(
    client: AsyncClient, settings: Settings
) -> None:
    origin = settings.CORS_ORIGINS[0]

    response = await client.get("/api/v1/health", headers={"Origin": origin})

    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"


async def test_cors_refuses_an_origin_that_is_not_configured(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health", headers={"Origin": "https://evil.test"})

    assert "access-control-allow-origin" not in response.headers


async def test_cors_names_its_methods_and_headers_rather_than_allowing_everything(
    client: AsyncClient, settings: Settings
) -> None:
    """A wildcard with credentials is both refused by browsers and wrong.

    Section 10 asks for CORS restricted to the frontend origin, and a policy
    that accepts any header from that origin is a smaller version of the same
    mistake.
    """
    origin = settings.CORS_ORIGINS[0]

    response = await client.options(
        "/api/v1/tasks",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    allowed_methods = response.headers["access-control-allow-methods"]
    allowed_headers = response.headers["access-control-allow-headers"]
    assert "*" not in allowed_methods
    assert "*" not in allowed_headers
    assert "POST" in allowed_methods
    assert "PUT" not in allowed_methods
