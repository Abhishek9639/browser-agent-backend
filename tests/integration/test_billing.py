"""Billing end to end against a real database (spec section 7).

The outbox is the interesting half, and it only means anything with a real
transaction: the claim being tested is that a business change and the call it
owes commit together or not at all.

The payments service is a fake FastAPI app in this process, reached through
httpx's ASGI transport, so the HTTP contract is exercised rather than mocked.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_app_settings,
    get_billing_service,
    get_screenshot_storage,
    get_task_runner,
)
from app.core.config import Settings
from app.domain.credits import Usage
from app.models.billing import OutboxKind, grant_key, release_key, settle_key
from app.repositories.unit_of_work import UnitOfWork
from app.services.billing import BillingService, OutboxWorker
from app.services.billing_client import BillingClient, InsufficientCredits
from app.services.storage import LocalFileStorage
from tests.integration.conftest_helpers import make_task, make_user

pytestmark = pytest.mark.integration

INSTRUCTION = "Find the cheapest flight from Delhi to Bengaluru on 20 Oct"
TOKEN = "an-internal-token"
RESERVATION_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")


class FakePayments:
    """The payments repository, as far as this one can tell."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.available = 500
        self.fail_times = 0
        self.app = self._build()

    @property
    def paths(self) -> list[str]:
        return [call["path"] for call in self.calls]

    def _maybe_fail(self) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise HTTPException(status_code=503, detail="not today")

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.post("/internal/v1/grants/signup", status_code=201)
        async def grant(request: Request) -> dict[str, Any]:
            body = await request.json()
            self.calls.append({"path": "grant", "body": body})
            self._maybe_fail()
            return {"granted": 200}

        @app.post("/internal/v1/reservations", status_code=201)
        async def reserve(request: Request) -> Any:
            body = await request.json()
            self.calls.append({"path": "reserve", "body": body})
            self._maybe_fail()
            amount = int(body["amount"])
            if amount > self.available:
                # Top level, not wrapped in FastAPI's `detail`: this shape is the
                # contract, and docs/billing-integration.md states it.
                return JSONResponse(
                    status_code=402,
                    content={"available": self.available, "required": amount},
                )
            return {"reservationId": str(RESERVATION_ID), "amount": amount}

        @app.post("/internal/v1/reservations/{reservation_id}/settle")
        async def settle(reservation_id: str, request: Request) -> dict[str, Any]:
            body = await request.json()
            self.calls.append({"path": "settle", "body": body})
            self._maybe_fail()
            return {"settled": body["actualAmount"]}

        @app.post("/internal/v1/reservations/{reservation_id}/release")
        async def release(reservation_id: str) -> dict[str, Any]:
            self.calls.append({"path": "release", "body": {}})
            self._maybe_fail()
            return {"released": True}

        return app


@pytest.fixture
def payments() -> FakePayments:
    return FakePayments()


@pytest.fixture
def billing_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "BILLING_ENABLED": True,
            "INTERNAL_API_TOKEN": SecretStr(TOKEN),
            "PAYMENTS_TIMEOUT_SECONDS": 0.5,
            "PAYMENTS_MAX_ATTEMPTS": 1,
            "BILLING_OUTBOX_BACKOFF_SECONDS": 0.01,
            "AGENT_MAX_STEPS": 25,
            "RESERVE_MULTIPLIER": 2,
        }
    )


@pytest.fixture
async def client(
    payments: FakePayments, billing_settings: Settings
) -> AsyncIterator[BillingClient]:
    transport = httpx.ASGITransport(app=payments.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://payments") as http:
        yield BillingClient(billing_settings, client=http)


def uow_factory(session: AsyncSession) -> Any:
    """A unit of work over the test transaction, which owns the commit."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[UnitOfWork]:
        yield UnitOfWork(session)

    return factory


# ------------------------------------------------------------------ the outbox


async def test_a_grant_is_queued_rather_than_called(
    db_session: AsyncSession, billing_settings: Settings, payments: FakePayments
) -> None:
    """Registration must not fail because payments is restarting."""
    user = await make_user(db_session, email="grant@example.test")
    service = BillingService(UnitOfWork(db_session), billing_settings)

    row = await service.queue_signup_grant(user.id)

    assert row is not None
    assert row.kind == OutboxKind.GRANT_SIGNUP.value
    assert row.idempotency_key == grant_key(user.id)
    assert row.done_at is None
    assert payments.calls == []


async def test_queueing_the_same_call_twice_is_one_row(
    db_session: AsyncSession, billing_settings: Settings
) -> None:
    """The unique key is the database refusing a double charge, rather than
    whoever remembered to check first."""
    user = await make_user(db_session, email="twice@example.test")
    service = BillingService(UnitOfWork(db_session), billing_settings)

    first = await service.queue_signup_grant(user.id)
    second = await service.queue_signup_grant(user.id)

    assert first is not None
    assert second is None
    uow = UnitOfWork(db_session)
    assert await uow.billing_outbox.pending_count() == 1


async def test_the_worker_delivers_and_marks_the_row_done(
    db_session: AsyncSession,
    billing_settings: Settings,
    payments: FakePayments,
    client: BillingClient,
) -> None:
    user = await make_user(db_session, email="deliver@example.test")
    uow = UnitOfWork(db_session)
    await BillingService(uow, billing_settings).queue_signup_grant(user.id)
    await db_session.commit()

    worker = OutboxWorker(uow_factory(db_session), billing_settings, client)
    delivered = await worker.drain()

    assert delivered == 1
    assert payments.paths == ["grant"]
    row = await uow.billing_outbox.for_key(grant_key(user.id))
    assert row is not None
    assert row.done_at is not None
    assert row.attempts == 1


async def test_the_worker_retries_until_it_succeeds(
    db_session: AsyncSession,
    billing_settings: Settings,
    payments: FakePayments,
    client: BillingClient,
) -> None:
    """The whole point of the table: payments being down costs time, not money."""
    user = await make_user(db_session, email="retry@example.test")
    uow = UnitOfWork(db_session)
    await BillingService(uow, billing_settings).queue_signup_grant(user.id)
    await db_session.commit()
    payments.fail_times = 2

    worker = OutboxWorker(uow_factory(db_session), billing_settings, client)

    assert await worker.drain() == 0
    row = await uow.billing_outbox.for_key(grant_key(user.id))
    assert row is not None
    assert row.attempts == 1
    assert row.done_at is None
    assert row.last_error

    # The backoff pushed it into the future, so nothing is due yet. Rewinding it
    # is what a test does instead of sleeping.
    row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    assert await worker.drain() == 0

    row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    assert await worker.drain() == 1

    assert row.done_at is not None
    assert row.attempts == 3


async def test_a_delivered_row_is_not_delivered_again(
    db_session: AsyncSession,
    billing_settings: Settings,
    payments: FakePayments,
    client: BillingClient,
) -> None:
    user = await make_user(db_session, email="once@example.test")
    uow = UnitOfWork(db_session)
    await BillingService(uow, billing_settings).queue_signup_grant(user.id)
    await db_session.commit()

    worker = OutboxWorker(uow_factory(db_session), billing_settings, client)
    await worker.drain()
    await worker.drain()

    assert payments.paths == ["grant"]


# ------------------------------------------------------------- the settlement


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (Usage(steps=7), 7),
        (Usage(steps=7, vision_steps=3), 10),
        (Usage(steps=4, replayed_steps=4), 2),
        (Usage(steps=5, vision_steps=1, replayed_steps=3), 5),
    ],
)
async def test_a_settlement_queues_the_computed_amount(
    db_session: AsyncSession,
    billing_settings: Settings,
    usage: Usage,
    expected: int,
) -> None:
    user = await make_user(db_session, email=f"settle{usage.steps}{usage.vision_steps}@x.test")
    task = await make_task(db_session, user, INSTRUCTION, credits_reservation_id=RESERVATION_ID)
    service = BillingService(UnitOfWork(db_session), billing_settings)

    row = await service.queue_settlement(task.id, RESERVATION_ID, usage)

    assert row is not None
    assert row.kind == OutboxKind.SETTLE.value
    assert row.payload["actualAmount"] == expected
    assert row.idempotency_key == settle_key(RESERVATION_ID)


async def test_a_run_with_no_steps_is_released_not_settled_at_zero(
    db_session: AsyncSession, billing_settings: Settings
) -> None:
    """Both leave the wallet in the same place. The ledger on the other side
    keeps "cost nothing" and "measured at nothing" apart."""
    user = await make_user(db_session, email="nosteps@example.test")
    task = await make_task(db_session, user, INSTRUCTION, credits_reservation_id=RESERVATION_ID)
    service = BillingService(UnitOfWork(db_session), billing_settings)

    row = await service.queue_settlement(task.id, RESERVATION_ID, Usage(steps=0))

    assert row is not None
    assert row.kind == OutboxKind.RELEASE.value
    assert row.idempotency_key == release_key(RESERVATION_ID)


async def test_a_task_with_no_reservation_queues_nothing(
    db_session: AsyncSession, billing_settings: Settings
) -> None:
    """Every task submitted while billing was off has none."""
    user = await make_user(db_session, email="noreservation@example.test")
    task = await make_task(db_session, user, INSTRUCTION)
    service = BillingService(UnitOfWork(db_session), billing_settings)

    assert await service.queue_settlement(task.id, None, Usage(steps=7)) is None


# --------------------------------------------------------------- the disabled


async def test_nothing_is_queued_when_billing_is_disabled(
    db_session: AsyncSession, settings: Settings, payments: FakePayments
) -> None:
    """Spec section 7: the agent works without the payments service."""
    user = await make_user(db_session, email="disabled@example.test")
    service = BillingService(UnitOfWork(db_session), settings)

    assert await service.queue_signup_grant(user.id) is None
    assert await service.reserve_for_task(user.id, uuid.uuid4()) is None
    assert await service.queue_settlement(uuid.uuid4(), RESERVATION_ID, Usage(steps=9)) is None

    uow = UnitOfWork(db_session)
    assert await uow.billing_outbox.pending_count() == 0
    assert payments.calls == []


async def test_the_worker_does_not_start_when_billing_is_disabled(
    db_session: AsyncSession, settings: Settings, client: BillingClient
) -> None:
    worker = OutboxWorker(uow_factory(db_session), settings, client)

    await worker.start()
    await worker.stop()

    # Nothing to assert beyond it not having run: start is a no-op and stop on a
    # worker that never started must not raise.
    assert settings.BILLING_ENABLED is False


# ------------------------------------------------------------------ the API


class FakeRunner:
    def __init__(self) -> None:
        self.submitted: list[uuid.UUID] = []

    def submit(self, task_id: uuid.UUID) -> None:
        self.submitted.append(task_id)

    def cancel_soon(self, task_id: uuid.UUID) -> bool:
        return True

    async def cancel(self, task_id: uuid.UUID) -> bool:
        return True

    def is_running(self, task_id: uuid.UUID) -> bool:
        return False


@pytest.fixture
def billing_app(
    api_app: FastAPI,
    settings: Settings,
    billing_settings: Settings,
    client: BillingClient,
    tmp_path: Path,
    db_session: AsyncSession,
) -> FastAPI:
    storage = LocalFileStorage(settings.model_copy(update={"STORAGE_DIR": str(tmp_path)}))
    # Overridden so every dependency sees billing enabled, including the
    # BillingService that AuthService builds for itself when a user registers.
    api_app.dependency_overrides[get_app_settings] = lambda: billing_settings
    api_app.dependency_overrides[get_task_runner] = lambda: FakeRunner()
    api_app.dependency_overrides[get_screenshot_storage] = lambda: storage
    api_app.dependency_overrides[get_billing_service] = lambda: BillingService(
        UnitOfWork(db_session), billing_settings, client=client
    )
    api_app.state.screenshot_storage = storage
    return api_app


@pytest.fixture
async def api(billing_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=billing_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def sign_in(api: httpx.AsyncClient, email: str) -> dict[str, str]:
    body = {"name": "Abhishek", "email": email, "password": "a-long-passphrase"}
    await api.post("/api/v1/auth/register", json=body)
    response = await api.post(
        "/api/v1/auth/login", json={"email": email, "password": body["password"]}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['accessToken']}"}


async def test_submitting_a_task_reserves_credits_first(
    api: httpx.AsyncClient, payments: FakePayments, db_session: AsyncSession
) -> None:
    auth = await sign_in(api, "reserve-ok@example.test")

    response = await api.post("/api/v1/tasks", json={"instruction": INSTRUCTION}, headers=auth)

    assert response.status_code == 201, response.text
    assert payments.paths == ["reserve"]
    # AGENT_MAX_STEPS x RESERVE_MULTIPLIER.
    assert payments.calls[0]["body"]["amount"] == 50

    uow = UnitOfWork(db_session)
    task = await uow.tasks.get_or_404(uuid.UUID(response.json()["id"]))
    assert task.credits_reservation_id == RESERVATION_ID


async def test_an_empty_wallet_is_a_402_with_both_numbers(
    api: httpx.AsyncClient, payments: FakePayments
) -> None:
    auth = await sign_in(api, "broke@example.test")
    payments.available = 10

    response = await api.post("/api/v1/tasks", json={"instruction": INSTRUCTION}, headers=auth)

    assert response.status_code == 402, response.text
    body = response.json()["error"]
    assert body["code"] == "INSUFFICIENT_CREDITS"
    assert body["details"] == {"available": 10, "required": 50}


async def test_payments_being_unreachable_is_a_503(
    api: httpx.AsyncClient, payments: FakePayments
) -> None:
    """A task that cannot be paid for must not run, and one whose payment is
    unknown must not either."""
    auth = await sign_in(api, "unreachable@example.test")
    payments.fail_times = 99

    response = await api.post("/api/v1/tasks", json={"instruction": INSTRUCTION}, headers=auth)

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "BILLING_UNAVAILABLE"


async def test_a_refusal_happens_before_the_task_is_committed(
    db_session: AsyncSession,
    billing_settings: Settings,
    client: BillingClient,
    payments: FakePayments,
    settings: Settings,
    tmp_path: Path,
) -> None:
    """The reservation is taken before the commit, so a 402 takes the row with it.

    Tested at the service rather than through the API, because the integration
    app deliberately replaces the request's unit of work with the test's own
    session and so cannot show a rollback.
    """
    from app.services.task_service import TaskService

    payments.available = 10
    user = await make_user(db_session, email="rollback@example.test")
    await db_session.commit()

    storage = LocalFileStorage(settings.model_copy(update={"STORAGE_DIR": str(tmp_path)}))
    uow = UnitOfWork(db_session)
    service = TaskService(
        uow,
        billing_settings,
        storage,
        billing=BillingService(uow, billing_settings, client=client),
    )

    with pytest.raises(InsufficientCredits):
        await service.create(user, instruction=INSTRUCTION)

    # What the request's unit of work would do on the way out.
    await db_session.rollback()
    assert await UnitOfWork(db_session).tasks.count() == 0


async def test_registering_queues_the_grant_in_the_same_transaction(
    api: httpx.AsyncClient, db_session: AsyncSession, payments: FakePayments
) -> None:
    """The user row and its grant commit together, or neither does."""
    await sign_in(api, "registered@example.test")

    uow = UnitOfWork(db_session)
    user = await uow.users.get_by_email("registered@example.test")
    assert user is not None
    row = await uow.billing_outbox.for_key(grant_key(user.id))
    assert row is not None
    assert row.done_at is None
    # Queued, not called: registration does not wait on payments.
    assert "grant" not in payments.paths


@pytest.mark.parametrize("available,expected", [(500, 201), (0, 402)])
async def test_replay_reserves_credits(
    api: httpx.AsyncClient,
    payments: FakePayments,
    db_session: AsyncSession,
    available: int,
    expected: int,
) -> None:
    from app.agent.cache.signature import task_signature
    from app.domain.enums import TaskStatus

    auth = await sign_in(api, "replay-billing@example.test")
    response = await api.post("/api/v1/tasks", json={"instruction": INSTRUCTION}, headers=auth)
    task_id = uuid.UUID(response.json()["id"])
    uow = UnitOfWork(db_session)
    task = await uow.tasks.get_or_404(task_id)
    task.status = TaskStatus.COMPLETED
    await uow.cached_flows.upsert(task_signature(INSTRUCTION), [], success_rate=1.0, run_count=1)
    await db_session.flush()
    payments.calls.clear()
    payments.available = available

    replay = await api.post(f"/api/v1/tasks/{task_id}/replay", headers=auth)

    assert replay.status_code == expected, replay.text
    assert payments.paths == ["reserve"]
    if expected == 201:
        replayed = await uow.tasks.get_or_404(uuid.UUID(replay.json()["id"]))
        assert replayed.credits_reservation_id == RESERVATION_ID
