"""The payments client (spec section 7).

Driven against a fake payments service in the same process, through httpx's ASGI
transport. A fake app rather than a mocked transport because the contract being
tested is HTTP: the status codes, the headers and the body shapes are the thing
the other repository has to implement, and `docs/billing-integration.md` is
written from these tests.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from pydantic import SecretStr

from app.core.config import Settings
from app.models.billing import grant_key, release_key, reserve_key, settle_key
from app.services.billing_client import (
    BillingClient,
    BillingUnavailable,
    InsufficientCredits,
)

USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
TASK_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
RESERVATION_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
TOKEN = "an-internal-token"


class FakePayments:
    """The other repository, as far as this one can tell.

    Records what it was sent, so a test can assert on the headers as well as the
    outcome: an idempotency key that is missing is a bug nobody notices until
    something is charged twice.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.available = 500
        self.fail_times = 0
        self.fail_status = 503
        self.app = self._build()

    @property
    def keys(self) -> list[str]:
        return [call["idempotency_key"] for call in self.calls]

    def _record(self, request: Request, path: str, body: dict[str, Any]) -> None:
        self.calls.append(
            {
                "path": path,
                "body": body,
                "idempotency_key": request.headers.get("Idempotency-Key"),
                "token": request.headers.get("X-Internal-Token"),
            }
        )

    def _maybe_fail(self) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise HTTPException(status_code=self.fail_status, detail="not today")

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.post("/internal/v1/grants/signup", status_code=201)
        async def grant(request: Request) -> dict[str, Any]:
            body = await request.json()
            self._record(request, "grant", body)
            self._maybe_fail()
            return {"granted": 200}

        @app.post("/internal/v1/reservations")
        async def reserve(request: Request) -> Any:
            body = await request.json()
            self._record(request, "reserve", body)
            self._maybe_fail()
            amount = int(body["amount"])
            if amount > self.available:
                raise HTTPException(
                    status_code=402,
                    detail={"available": self.available, "required": amount},
                )
            return {"reservationId": str(RESERVATION_ID), "amount": amount}

        @app.post("/internal/v1/reservations/{reservation_id}/settle", status_code=200)
        async def settle(reservation_id: str, request: Request) -> dict[str, Any]:
            body = await request.json()
            self._record(request, "settle", body)
            self._maybe_fail()
            return {"reservationId": reservation_id, "settled": body["actualAmount"]}

        @app.post("/internal/v1/reservations/{reservation_id}/release", status_code=200)
        async def release(reservation_id: str, request: Request) -> dict[str, Any]:
            self._record(request, "release", {})
            self._maybe_fail()
            return {"reservationId": reservation_id, "released": True}

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
            "PAYMENTS_MAX_ATTEMPTS": 3,
        }
    )


@pytest.fixture
async def client(
    payments: FakePayments, billing_settings: Settings
) -> AsyncIterator[BillingClient]:
    transport = httpx.ASGITransport(app=payments.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://payments") as http:
        yield BillingClient(billing_settings, client=http)


# ------------------------------------------------------------------ the calls


async def test_a_signup_grant_is_sent_with_a_key_per_user(
    client: BillingClient, payments: FakePayments
) -> None:
    await client.grant_signup(USER_ID)

    assert payments.calls[0]["path"] == "grant"
    assert payments.calls[0]["body"] == {"userId": str(USER_ID)}
    assert payments.keys == [grant_key(USER_ID)]
    assert payments.calls[0]["token"] == TOKEN


async def test_a_reservation_comes_back_with_its_id(
    client: BillingClient, payments: FakePayments
) -> None:
    reservation = await client.reserve(USER_ID, TASK_ID, 50)

    assert reservation.reservation_id == RESERVATION_ID
    assert reservation.amount == 50
    assert payments.keys == [reserve_key(TASK_ID)]


async def test_settle_and_release_are_keyed_on_the_reservation(
    client: BillingClient, payments: FakePayments
) -> None:
    await client.settle(RESERVATION_ID, 9)
    await client.release(RESERVATION_ID)

    assert payments.calls[0]["body"] == {"actualAmount": 9}
    assert payments.keys == [settle_key(RESERVATION_ID), release_key(RESERVATION_ID)]


# -------------------------------------------------------------- the refusals


async def test_an_insufficient_balance_carries_both_numbers(
    billing_settings: Settings,
) -> None:
    """A bare "payment required" sends somebody to a wallet page to work out
    how short they are."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"available": 12, "required": 50})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://payments") as http:
        client = BillingClient(billing_settings, client=http)

        with pytest.raises(InsufficientCredits) as raised:
            await client.reserve(USER_ID, TASK_ID, 50)

    assert raised.value.available == 12
    assert raised.value.required == 50
    assert raised.value.http_status == 402
    assert raised.value.details == {"available": 12, "required": 50}


async def test_a_402_without_numbers_is_still_a_refusal(billing_settings: Settings) -> None:
    """A broken body makes the message vaguer, never the answer softer."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, content=b"not json")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://payments") as http:
        client = BillingClient(billing_settings, client=http)

        with pytest.raises(InsufficientCredits):
            await client.reserve(USER_ID, TASK_ID, 50)


async def test_a_402_is_not_retried(billing_settings: Settings) -> None:
    """It is an answer. Asking again reaches the same one and wastes the wait."""
    seen = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        return httpx.Response(402, json={"available": 0, "required": 50})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://payments") as http:
        client = BillingClient(billing_settings, client=http)
        with pytest.raises(InsufficientCredits):
            await client.reserve(USER_ID, TASK_ID, 50)

    assert seen == 1


async def test_a_201_without_a_reservation_id_is_unavailable(
    billing_settings: Settings,
) -> None:
    """A broken contract, not a missing feature. A task must not run unpaid."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"amount": 50})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://payments") as http:
        client = BillingClient(billing_settings, client=http)

        with pytest.raises(BillingUnavailable):
            await client.reserve(USER_ID, TASK_ID, 50)


# --------------------------------------------------------------- the retries


async def test_a_5xx_is_retried_and_then_succeeds(
    client: BillingClient, payments: FakePayments
) -> None:
    payments.fail_times = 2

    await client.grant_signup(USER_ID)

    assert len(payments.calls) == 3
    # The same key every time, which is what makes a retry safe: a grant applied
    # twice is free credits.
    assert set(payments.keys) == {grant_key(USER_ID)}


async def test_retries_run_out_and_become_unavailable(
    client: BillingClient, payments: FakePayments
) -> None:
    payments.fail_times = 99

    with pytest.raises(BillingUnavailable):
        await client.grant_signup(USER_ID)

    assert len(payments.calls) == 3


async def test_a_4xx_other_than_402_is_not_retried(
    client: BillingClient, payments: FakePayments
) -> None:
    payments.fail_times = 99
    payments.fail_status = 400

    with pytest.raises(BillingUnavailable):
        await client.grant_signup(USER_ID)

    assert len(payments.calls) == 1


async def test_payments_being_unreachable_is_unavailable(billing_settings: Settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://payments") as http:
        client = BillingClient(billing_settings, client=http)

        with pytest.raises(BillingUnavailable) as raised:
            await client.reserve(USER_ID, TASK_ID, 50)

    assert raised.value.http_status == 503


async def test_a_timeout_is_retried_because_the_call_may_have_landed(
    billing_settings: Settings,
) -> None:
    """The case the idempotency key exists for.

    A timeout means the answer was lost, not that nothing happened, so the
    retry has to be the same request rather than a second one.
    """
    keys: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers.get("Idempotency-Key"))
        if len(keys) < 3:
            raise httpx.ReadTimeout("took too long")
        return httpx.Response(201, json={"reservationId": str(RESERVATION_ID), "amount": 50})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://payments") as http:
        client = BillingClient(billing_settings, client=http)
        reservation = await client.reserve(USER_ID, TASK_ID, 50)

    assert reservation.reservation_id == RESERVATION_ID
    assert keys == [reserve_key(TASK_ID)] * 3


async def test_no_token_header_when_none_is_configured(settings: Settings) -> None:
    """A deployment without a shared secret should not send an empty one."""
    seen: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-Internal-Token"))
        return httpx.Response(201, json={"granted": 200})

    bare = settings.model_copy(update={"BILLING_ENABLED": True, "INTERNAL_API_TOKEN": None})
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://payments") as http:
        await BillingClient(bare, client=http).grant_signup(USER_ID)

    assert seen == [None]


def test_the_idempotency_keys_are_stable_and_distinct() -> None:
    """They are the only thing standing between a retry and a double charge."""
    assert grant_key(USER_ID) == f"signup:{USER_ID}"
    assert reserve_key(TASK_ID) == f"reserve:{TASK_ID}"
    assert settle_key(RESERVATION_ID) != release_key(RESERVATION_ID)
