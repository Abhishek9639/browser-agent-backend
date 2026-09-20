"""B07: refresh rotation, reuse detection, logout, RBAC and rate limiting."""

from typing import Any

import pytest
from fastapi import Depends, FastAPI
from httpx import AsyncClient
from sqlalchemy import text

from app.api.deps import require_roles
from app.core.config import Settings
from app.domain.enums import UserRole

VALID = {"name": "Abhishek", "email": "abhishek@example.test", "password": "a-long-passphrase"}
COOKIE = "refresh_token"


async def register(client: AsyncClient, **overrides: Any) -> Any:
    return await client.post("/api/v1/auth/register", json={**VALID, **overrides})


async def login(client: AsyncClient, **overrides: Any) -> Any:
    body = {"email": VALID["email"], "password": VALID["password"], **overrides}
    return await client.post("/api/v1/auth/login", json=body)


async def post_with_cookie(client: AsyncClient, url: str, cookie: str | None) -> Any:
    """POST while presenting a specific refresh cookie.

    Per-request `cookies=` is deprecated in httpx, so the client's own jar is
    swapped instead. Needed to replay an old cookie the client no longer holds.
    """
    original = client.cookies.get(COOKIE)
    if cookie is None:
        client.cookies.delete(COOKIE)
    else:
        client.cookies.set(COOKIE, cookie)
    try:
        return await client.post(url)
    finally:
        if original is None:
            client.cookies.delete(COOKIE)
        else:
            client.cookies.set(COOKIE, original)


async def signed_in(client: AsyncClient) -> str:
    await register(client)
    response = await login(client)
    return str(response.json()["accessToken"])


# ------------------------------------------------------------------ cookie


async def test_login_sets_the_refresh_cookie(api_client: AsyncClient) -> None:
    await register(api_client)

    response = await login(api_client)

    assert response.status_code == 200
    assert api_client.cookies.get(COOKIE)


async def test_cookie_is_httponly_lax_and_scoped_to_auth(api_client: AsyncClient) -> None:
    """httpOnly so XSS cannot read it; path so it is not sent everywhere."""
    await register(api_client)

    header = (await login(api_client)).headers["set-cookie"].lower()

    assert "httponly" in header
    assert "samesite=lax" in header
    assert "path=/api/v1/auth" in header


async def test_cookie_is_not_secure_in_dev(api_client: AsyncClient) -> None:
    """Secure would drop the cookie over plain http and break local dev."""
    await register(api_client)

    header = (await login(api_client)).headers["set-cookie"].lower()

    assert "secure" not in header


async def test_refresh_token_is_never_in_the_response_body(api_client: AsyncClient) -> None:
    await register(api_client)

    response = await login(api_client)

    assert "refreshToken" not in response.text
    assert api_client.cookies.get(COOKIE) not in response.text


async def test_only_the_hash_is_stored(api_client: AsyncClient, db_session: Any) -> None:
    await register(api_client)
    await login(api_client)
    raw = api_client.cookies.get(COOKIE)

    stored = (await db_session.scalars(text("SELECT token_hash FROM refresh_tokens"))).all()

    assert stored
    assert raw not in stored
    assert all(len(h) == 64 for h in stored)


# ------------------------------------------------------------------ rotation


async def test_refresh_returns_a_new_access_token(api_client: AsyncClient) -> None:
    await signed_in(api_client)

    response = await api_client.post("/api/v1/auth/refresh")

    assert response.status_code == 200
    body = response.json()
    assert body["accessToken"]
    assert body["expiresIn"] == 900
    assert body["user"]["email"] == "abhishek@example.test"


async def test_refresh_rotates_the_cookie(api_client: AsyncClient) -> None:
    await signed_in(api_client)
    before = api_client.cookies.get(COOKIE)

    await api_client.post("/api/v1/auth/refresh")

    assert api_client.cookies.get(COOKIE) != before


async def test_rotation_chain_is_recorded(api_client: AsyncClient, db_session: Any) -> None:
    """replaced_by_id is what makes reuse detectable.

    Not ordered by created_at: Postgres `now()` is the *transaction* timestamp,
    so two rows written in one transaction carry the same value and the order
    between them is undefined.
    """
    await signed_in(api_client)
    await api_client.post("/api/v1/auth/refresh")

    rows = (
        await db_session.execute(
            text("SELECT revoked_at IS NOT NULL, replaced_by_id IS NOT NULL FROM refresh_tokens")
        )
    ).all()

    assert len(rows) == 2
    assert sorted(rows) == [(False, False), (True, True)], (
        "one token revoked and pointing at its successor, one live"
    )


async def test_refresh_can_be_repeated(api_client: AsyncClient) -> None:
    await signed_in(api_client)

    for _ in range(3):
        assert (await api_client.post("/api/v1/auth/refresh")).status_code == 200


async def test_refresh_without_a_cookie_is_401(api_client: AsyncClient) -> None:
    response = await api_client.post("/api/v1/auth/refresh")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


async def test_refresh_with_an_unknown_token_is_401(api_client: AsyncClient) -> None:
    response = await post_with_cookie(api_client, "/api/v1/auth/refresh", "not-a-real-token")

    assert response.status_code == 401


# ------------------------------------------------------------ reuse detection


async def test_replaying_a_rotated_token_is_401(api_client: AsyncClient) -> None:
    await signed_in(api_client)
    stolen = api_client.cookies.get(COOKIE)
    await api_client.post("/api/v1/auth/refresh")

    response = await post_with_cookie(api_client, "/api/v1/auth/refresh", stolen)

    assert response.status_code == 401


async def test_reuse_revokes_the_whole_chain(api_client: AsyncClient, db_session: Any) -> None:
    """Two parties hold one cookie and we cannot tell which is the thief.

    So every session for that user dies, and the real user logs in again.
    """
    await signed_in(api_client)
    stolen = api_client.cookies.get(COOKIE)
    await api_client.post("/api/v1/auth/refresh")

    await post_with_cookie(api_client, "/api/v1/auth/refresh", stolen)

    live = await db_session.scalar(
        text("SELECT count(*) FROM refresh_tokens WHERE revoked_at IS NULL")
    )
    assert live == 0


async def test_the_legitimate_session_dies_too(api_client: AsyncClient) -> None:
    """The cost of the policy, and it is the right cost."""
    await signed_in(api_client)
    stolen = api_client.cookies.get(COOKIE)
    await api_client.post("/api/v1/auth/refresh")

    await post_with_cookie(api_client, "/api/v1/auth/refresh", stolen)

    assert (await api_client.post("/api/v1/auth/refresh")).status_code == 401


async def test_reuse_is_audited(api_client: AsyncClient, db_session: Any) -> None:
    """Written in the request transaction, which the route lets commit."""
    await signed_in(api_client)
    stolen = api_client.cookies.get(COOKIE)
    await api_client.post("/api/v1/auth/refresh")

    await post_with_cookie(api_client, "/api/v1/auth/refresh", stolen)

    rows = (
        await db_session.scalars(
            text("SELECT metadata::text FROM audit_events WHERE action = 'TOKEN_REUSE_DETECTED'")
        )
    ).all()

    assert len(rows) == 1
    assert "sessions_revoked" in rows[0]
    # session_id, not token_id: the redaction rule would have masked the latter.
    assert "[REDACTED]" not in rows[0]


# ------------------------------------------------------------------ logout


async def test_logout_clears_the_cookie(api_client: AsyncClient) -> None:
    await signed_in(api_client)

    response = await api_client.post("/api/v1/auth/logout")

    assert response.status_code == 204
    assert not api_client.cookies.get(COOKIE)


async def test_logout_revokes_the_token(api_client: AsyncClient, db_session: Any) -> None:
    await signed_in(api_client)

    await api_client.post("/api/v1/auth/logout")

    live = await db_session.scalar(
        text("SELECT count(*) FROM refresh_tokens WHERE revoked_at IS NULL")
    )
    assert live == 0


async def test_refresh_after_logout_is_401(api_client: AsyncClient) -> None:
    await signed_in(api_client)
    cookie = api_client.cookies.get(COOKIE)
    await api_client.post("/api/v1/auth/logout")

    assert (await post_with_cookie(api_client, "/api/v1/auth/refresh", cookie)).status_code == 401


async def test_logout_without_a_cookie_is_still_204(api_client: AsyncClient) -> None:
    """Idempotent: a client that lost its cookie has already logged out."""
    assert (await api_client.post("/api/v1/auth/logout")).status_code == 204


async def test_logging_out_twice_is_fine(api_client: AsyncClient) -> None:
    await signed_in(api_client)
    cookie = api_client.cookies.get(COOKIE)

    await post_with_cookie(api_client, "/api/v1/auth/logout", cookie)

    assert (await post_with_cookie(api_client, "/api/v1/auth/logout", cookie)).status_code == 204


# ------------------------------------------------------------------ /me


async def test_me_returns_the_authenticated_user(api_client: AsyncClient) -> None:
    token = await signed_in(api_client)

    response = await api_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["email"] == "abhishek@example.test"
    assert "passwordHash" not in response.text


async def test_me_without_a_token_is_401(api_client: AsyncClient) -> None:
    assert (await api_client.get("/api/v1/auth/me")).status_code == 401


# ------------------------------------------------------------------ rbac


@pytest.fixture
def rbac_app(api_app: FastAPI) -> FastAPI:
    @api_app.get("/api/v1/admin-only", dependencies=[Depends(require_roles(UserRole.ADMIN))])
    async def admin_only() -> dict[str, bool]:
        return {"ok": True}

    @api_app.get(
        "/api/v1/approvers",
        dependencies=[Depends(require_roles(UserRole.APPROVER, UserRole.ADMIN))],
    )
    async def approvers() -> dict[str, bool]:
        return {"ok": True}

    return api_app


async def test_unauthenticated_is_401_not_403(rbac_app: FastAPI, api_client: AsyncClient) -> None:
    """401 means we do not know you; 403 means we do and you may not."""
    response = await api_client.get("/api/v1/admin-only")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


async def test_wrong_role_is_403(rbac_app: FastAPI, api_client: AsyncClient) -> None:
    token = await signed_in(api_client)

    response = await api_client.get(
        "/api/v1/admin-only", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


async def test_admin_is_not_implied_by_other_roles(
    rbac_app: FastAPI, api_client: AsyncClient
) -> None:
    """Every route lists the roles it accepts; nothing is granted implicitly."""
    token = await signed_in(api_client)

    allowed = await api_client.get(
        "/api/v1/approvers", headers={"Authorization": f"Bearer {token}"}
    )

    assert allowed.status_code == 403, "a TASK_SUBMITTER is not an approver"


async def test_an_admin_passes(rbac_app: FastAPI, api_client: AsyncClient, db_session: Any) -> None:
    await register(api_client)
    await db_session.execute(text("UPDATE users SET role = 'ADMIN'"))
    await db_session.flush()
    token = (await login(api_client)).json()["accessToken"]

    response = await api_client.get(
        "/api/v1/admin-only", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200


# ------------------------------------------------------------------ throttling


async def test_login_is_limited_to_five_a_minute(api_client: AsyncClient) -> None:
    await register(api_client)

    codes = [(await login(api_client, password="wrong-passphrase")).status_code for _ in range(6)]

    assert codes[:5] == [401] * 5
    assert codes[5] == 429


async def test_429_uses_the_error_envelope_and_retry_after(api_client: AsyncClient) -> None:
    await register(api_client)
    for _ in range(5):
        await login(api_client, password="wrong-passphrase")

    response = await login(api_client, password="wrong-passphrase")

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert response.json()["error"]["requestId"].startswith("req_")
    assert int(response.headers["Retry-After"]) >= 1


async def test_register_is_limited_to_three_a_minute(api_client: AsyncClient) -> None:
    codes = [
        (await register(api_client, email=f"user{index}@example.test")).status_code
        for index in range(4)
    ]

    assert codes[:3] == [201] * 3
    assert codes[3] == 429


async def test_login_and_register_have_separate_buckets(api_client: AsyncClient) -> None:
    """Exhausting one must not lock the other."""
    for index in range(3):
        await register(api_client, email=f"user{index}@example.test")
    assert (await register(api_client, email="blocked@example.test")).status_code == 429

    assert (await login(api_client, email="user0@example.test")).status_code == 200


async def test_the_limit_resets_as_the_bucket_refills(
    api_app: FastAPI, api_client: AsyncClient
) -> None:
    """Reset the bucket directly rather than sleeping twelve seconds."""
    await register(api_client)
    for _ in range(5):
        await login(api_client, password="wrong-passphrase")
    assert (await login(api_client)).status_code == 429

    await api_app.state.rate_limiter.reset()

    assert (await login(api_client)).status_code == 200


async def test_limiting_can_be_switched_off(
    settings: Settings, db_session: Any, jwt_key_dir: Any
) -> None:
    """RATE_LIMIT_ENABLED=false for load tests and local experimentation."""
    from httpx import ASGITransport

    from app.core.jwt_keys import load_key_pair
    from app.db.session import Database, get_database, get_session
    from app.main import create_app

    relaxed = settings.model_copy(update={"RATE_LIMIT_ENABLED": False})
    app = create_app(relaxed)
    app.state.jwt_keys = load_key_pair(relaxed)
    database = Database(relaxed, url=relaxed.test_database_url)
    app.state.db = database
    app.dependency_overrides[get_session] = lambda: db_session
    app.dependency_overrides[get_database] = lambda: database

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        codes = [
            (
                await client.post("/api/v1/auth/register", json={**VALID, "email": f"u{i}@x.test"})
            ).status_code
            for i in range(6)
        ]

    assert 429 not in codes
