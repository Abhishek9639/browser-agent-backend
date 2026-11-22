"""Real, independently committed requests, unlike the shared savepoint fixtures."""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.config import Settings
from app.core.jwt_keys import load_key_pair
from app.db.session import Database
from app.main import create_app
from app.models.audit import AuditEvent
from app.models.user import User
from app.repositories.unit_of_work import UnitOfWork
from app.repositories.user import UserRepository


@pytest.fixture
async def committed_auth(settings: Settings, integration_settings: Settings) -> AsyncIterator:
    database = Database(settings, url=integration_settings.test_database_url)
    application = create_app(settings.model_copy(update={"RATE_LIMIT_ENABLED": False}))
    application.state.db = database
    application.state.jwt_keys = load_key_pair(settings)
    email = f"concurrency-{uuid.uuid4().hex}@example.test"
    transport = ASGITransport(app=application, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, database, email
    finally:
        async with database.session() as session:
            user = await UnitOfWork(session).users.get_by_email(email)
            if user is not None:
                await session.execute(delete(AuditEvent).where(AuditEvent.actor_id == user.id))
                await session.execute(delete(User).where(User.id == user.id))
        await database.dispose()


async def test_simultaneous_refresh_mints_only_one_successor(committed_auth):
    client, database, email = committed_auth
    body = {"name": "Concurrent", "email": email, "password": "a-long-passphrase"}
    assert (await client.post("/api/v1/auth/register", json=body)).status_code == 201
    assert (await client.post("/api/v1/auth/login", json=body)).status_code == 200
    original = client.cookies.get("refresh_token")
    headers = {"Cookie": f"refresh_token={original}"}
    responses = await asyncio.gather(
        *[client.post("/api/v1/auth/refresh", headers=headers) for _ in range(2)]
    )
    assert [r.status_code for r in responses] == [200, 200]
    assert sum("set-cookie" in r.headers for r in responses) == 1
    async with database.session() as session:
        uow = UnitOfWork(session)
        user = await uow.users.get_by_email(email)
        assert len(await uow.refresh_tokens.list_active_for_user(user.id)) == 1


async def test_competing_registration_returns_conflict(committed_auth, monkeypatch):
    client, _database, email = committed_auth
    barrier = asyncio.Barrier(2)
    original = UserRepository.email_exists

    async def simultaneous_lookup(self, address):
        found = await original(self, address)
        await barrier.wait()
        return found

    monkeypatch.setattr(UserRepository, "email_exists", simultaneous_lookup)
    body = {"name": "Concurrent", "email": email, "password": "a-long-passphrase"}
    responses = await asyncio.gather(
        *[client.post("/api/v1/auth/register", json=body) for _ in range(2)]
    )
    assert sorted(r.status_code for r in responses) == [201, 409]


async def test_long_user_agent_does_not_break_login(committed_auth):
    client, _database, email = committed_auth
    body = {"name": "Header", "email": email, "password": "a-long-passphrase"}
    assert (await client.post("/api/v1/auth/register", json=body)).status_code == 201
    response = await client.post(
        "/api/v1/auth/login", json=body, headers={"User-Agent": "x" * 1024}
    )
    assert response.status_code == 200, response.text
    assert (await client.post("/api/v1/auth/refresh")).status_code == 200
