"""B06: register, login, JWKS and the current-user dependency, end to end."""

import json
from typing import Any

import jwt
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.user import User

VALID = {"name": "Abhishek", "email": "abhishek@example.test", "password": "a-long-passphrase"}


async def register(client: AsyncClient, **overrides: Any) -> Any:
    return await client.post("/api/v1/auth/register", json={**VALID, **overrides})


async def login(client: AsyncClient, **overrides: Any) -> Any:
    body = {"email": VALID["email"], "password": VALID["password"], **overrides}
    return await client.post("/api/v1/auth/login", json=body)


# ------------------------------------------------------------------ register


async def test_register_creates_a_task_submitter(api_client: AsyncClient) -> None:
    response = await register(api_client)

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "abhishek@example.test"
    assert body["name"] == "Abhishek"
    assert body["role"] == "TASK_SUBMITTER"
    assert body["id"]
    assert body["createdAt"]


async def test_register_never_returns_the_hash(api_client: AsyncClient) -> None:
    response = await register(api_client)

    assert "passwordHash" not in response.json()
    assert "password_hash" not in response.text
    assert VALID["password"] not in response.text


async def test_email_is_normalised_to_lowercase(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await register(api_client, email="  Abhishek@Example.TEST  ")

    assert response.status_code == 201
    assert response.json()["email"] == "abhishek@example.test"
    stored = await db_session.scalar(select(User.email))
    assert stored == "abhishek@example.test"


async def test_duplicate_email_is_409(api_client: AsyncClient) -> None:
    await register(api_client)

    response = await register(api_client, name="Someone Else")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


async def test_duplicate_is_detected_across_casing(api_client: AsyncClient) -> None:
    """Normalising on the way in is what makes this work."""
    await register(api_client)

    response = await register(api_client, email="ABHISHEK@EXAMPLE.TEST")

    assert response.status_code == 409


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("password", "short"),
        ("password", "123456789012"),
        ("password", "   "),
        ("email", "not-an-email"),
        ("email", "missing@domain"),
        ("email", ""),
        ("name", ""),
        ("name", "   "),
    ],
)
async def test_register_validation(api_client: AsyncClient, field: str, value: str) -> None:
    response = await register(api_client, **{field: value})

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_all_digit_password_is_rejected(api_client: AsyncClient) -> None:
    """Long enough, but a pure number is trivially guessable."""
    response = await register(api_client, password="1234567890123456")

    assert response.status_code == 400


async def test_register_writes_an_audit_event(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    await register(api_client)

    actions = (await db_session.scalars(text("SELECT action FROM audit_events"))).all()
    assert "USER_REGISTERED" in actions


async def test_the_password_is_never_stored_in_clear(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    await register(api_client)

    stored = await db_session.scalar(select(User.password_hash))
    assert stored is not None
    assert stored.startswith("$argon2id$")
    assert VALID["password"] not in stored


# ------------------------------------------------------------------ login


async def test_login_returns_a_token_and_the_user(api_client: AsyncClient) -> None:
    await register(api_client)

    response = await login(api_client)

    assert response.status_code == 200
    body = response.json()
    assert body["tokenType"] == "Bearer"
    assert body["expiresIn"] == 900
    assert body["accessToken"]
    assert body["user"]["email"] == "abhishek@example.test"
    assert "passwordHash" not in body["user"]


async def test_login_token_is_usable(api_client: AsyncClient, settings: Settings) -> None:
    await register(api_client)
    token = (await login(api_client)).json()["accessToken"]

    claims = jwt.decode(
        token,
        options={"verify_signature": False},
        audience=settings.JWT_AUDIENCE,
    )

    assert claims["role"] == "TASK_SUBMITTER"


async def test_login_is_case_insensitive_on_email(api_client: AsyncClient) -> None:
    await register(api_client)

    response = await login(api_client, email="ABHISHEK@Example.test")

    assert response.status_code == 200


async def test_wrong_password_is_401(api_client: AsyncClient) -> None:
    await register(api_client)

    response = await login(api_client, password="a-different-passphrase")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_unknown_email_is_401(api_client: AsyncClient) -> None:
    response = await login(api_client, email="nobody@example.test")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_unknown_email_and_wrong_password_are_indistinguishable(
    api_client: AsyncClient,
) -> None:
    """The login form must not become an account-existence oracle."""
    await register(api_client)

    wrong_password = await login(api_client, password="a-different-passphrase")
    no_such_user = await login(api_client, email="nobody@example.test")

    assert wrong_password.status_code == no_such_user.status_code == 401
    assert wrong_password.json()["error"]["code"] == no_such_user.json()["error"]["code"]
    assert wrong_password.json()["error"]["message"] == no_such_user.json()["error"]["message"]


async def test_a_malformed_email_fails_as_credentials_not_validation(
    api_client: AsyncClient,
) -> None:
    """Otherwise a 400 would reveal the address was never registrable."""
    response = await login(api_client, email="not-an-email")

    assert response.status_code == 401


async def test_login_never_echoes_the_password(api_client: AsyncClient) -> None:
    await register(api_client)

    response = await login(api_client, password="wrong-but-memorable")

    assert "wrong-but-memorable" not in response.text


async def test_successful_login_is_audited(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    await register(api_client)
    await login(api_client)

    actions = (await db_session.scalars(text("SELECT action FROM audit_events"))).all()
    assert "USER_LOGGED_IN" in actions


# ------------------------------------------------------------------ jwks


async def test_jwks_is_served_at_the_root(api_client: AsyncClient) -> None:
    """RFC 8615, and spec section 6 says root path explicitly."""
    response = await api_client.get("/.well-known/jwks.json")

    assert response.status_code == 200
    assert (await api_client.get("/api/v1/.well-known/jwks.json")).status_code == 404


async def test_jwks_needs_no_authentication(api_client: AsyncClient) -> None:
    """Payments fetches this without a token."""
    assert (await api_client.get("/.well-known/jwks.json")).status_code == 200


async def test_jwks_shape(api_client: AsyncClient, settings: Settings) -> None:
    document = (await api_client.get("/.well-known/jwks.json")).json()

    assert list(document) == ["keys"]
    jwk = document["keys"][0]
    assert jwk["kty"] == "RSA"
    assert jwk["use"] == "sig"
    assert jwk["alg"] == "RS256"
    assert jwk["kid"] == settings.JWT_KID
    assert set(jwk) == {"kty", "kid", "use", "alg", "n", "e"}


async def test_a_login_token_verifies_against_the_published_jwks(
    api_client: AsyncClient, settings: Settings
) -> None:
    """The whole payments integration, end to end, over HTTP.

    Nothing here touches a PEM: only the token and the published document.
    """
    await register(api_client)
    token = (await login(api_client)).json()["accessToken"]
    document = (await api_client.get("/.well-known/jwks.json")).json()

    kid = jwt.get_unverified_header(token)["kid"]
    matching = next(k for k in document["keys"] if k["kid"] == kid)
    public_key = RSAAlgorithm.from_jwk(json.dumps(matching))

    claims = jwt.decode(
        token,
        public_key,  # type: ignore[arg-type]
        algorithms=["RS256"],
        issuer=settings.JWT_ISSUER,
        audience=settings.JWT_AUDIENCE,
    )

    assert claims["sub"]
    assert claims["role"] == "TASK_SUBMITTER"


# ------------------------------------------------------------------ bearer


async def test_protected_route_accepts_a_valid_token(
    api_app: FastAPI, api_client: AsyncClient
) -> None:
    from app.api.deps import CurrentUser

    @api_app.get("/api/v1/whoami")
    async def whoami(user: CurrentUser) -> dict[str, str]:
        return {"email": user.email, "role": user.role.value}

    await register(api_client)
    token = (await login(api_client)).json()["accessToken"]

    response = await api_client.get("/api/v1/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"email": "abhishek@example.test", "role": "TASK_SUBMITTER"}


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", "Bearer   ", "Token abc", "Bearer not-a-jwt"],
)
async def test_protected_route_rejects_bad_authorization(
    api_app: FastAPI, api_client: AsyncClient, header: str | None
) -> None:
    from app.api.deps import CurrentUser

    @api_app.get("/api/v1/whoami-2")
    async def whoami(user: CurrentUser) -> dict[str, str]:
        return {"email": user.email}

    headers = {} if header is None else {"Authorization": header}
    response = await api_client.get("/api/v1/whoami-2", headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] in {"UNAUTHORIZED", "INVALID_CREDENTIALS"}


# ------------------------------------------------------- failed-login audit


async def test_failed_login_audit_survives_the_rollback(
    api_client: AsyncClient, db_engine: Any
) -> None:
    """A 401 rolls the request back, so this row needs its own transaction.

    Read through a separate connection: if it is visible there, it really was
    committed rather than merely staged in the test's transaction.
    """
    await register(api_client)

    response = await login(api_client, password="definitely-not-the-password")
    assert response.status_code == 401

    async with db_engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT action, metadata::text FROM audit_events WHERE action = 'LOGIN_FAILED'"
                )
            )
        ).all()

    assert len(rows) == 1
    assert rows[0][0] == "LOGIN_FAILED"
    assert "definitely-not-the-password" not in rows[0][1]
    assert "abhishek@example.test" in rows[0][1]


async def test_unknown_email_is_also_audited(api_client: AsyncClient, db_engine: Any) -> None:
    await login(api_client, email="ghost@example.test")

    async with db_engine.connect() as connection:
        reasons = (
            (
                await connection.execute(
                    text(
                        "SELECT metadata->>'reason' FROM audit_events WHERE action = 'LOGIN_FAILED'"
                    )
                )
            )
            .scalars()
            .all()
        )

    assert reasons == ["unknown_email"]


async def test_failed_login_audit_has_no_actor(api_client: AsyncClient, db_engine: Any) -> None:
    """No authenticated actor: the attempt failed."""
    await login(api_client, email="ghost@example.test")

    async with db_engine.connect() as connection:
        actors = (
            (
                await connection.execute(
                    text("SELECT actor_id FROM audit_events WHERE action = 'LOGIN_FAILED'")
                )
            )
            .scalars()
            .all()
        )

    assert actors == [None]
