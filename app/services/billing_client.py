"""The internal client for the payments service (spec section 7).

The payments service is a separate repository with its own database. This is
the only thing in this repository that talks to it, and the contract is written
down in ``docs/billing-integration.md`` so the other side can be implemented
against it rather than against this code.

Four calls, and the differences between them matter more than the similarities.

``reserve`` is **synchronous and on the request path**: a user submitting a task
has to be told now whether they can afford it, so a 402 has to reach them as a
402. Everything else is **asynchronous through the outbox**, because a grant or
a settle that is lost because payments was restarting is money that silently
went missing.

Every call carries an ``Idempotency-Key``. It is not decoration: the retries
below mean any of these can arrive twice, and a grant applied twice is free
credits while a settle applied twice is a double charge.
"""

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Final, Self

import httpx

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.models.billing import grant_key, release_key, reserve_key, settle_key

__all__ = [
    "BillingClient",
    "BillingUnavailable",
    "InsufficientCredits",
    "Reservation",
]

logger = get_logger(__name__)

INTERNAL_PREFIX: Final = "/internal/v1"
TOKEN_HEADER: Final = "X-Internal-Token"  # noqa: S105 - a header name
IDEMPOTENCY_HEADER: Final = "Idempotency-Key"

# Retried: the network did not reach an opinion, or the far side had an accident.
# Never a 4xx, because a 402 is an answer and repeating the question does not
# change it.
RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({500, 502, 503, 504})


class InsufficientCredits(AppError):
    """The wallet cannot cover the reservation (spec section 7: 402).

    Carries the two numbers a user needs to act: what they have and what the
    task would hold. A bare "payment required" tells somebody to go and look at
    a wallet page to find out how short they are.
    """

    http_status = 402
    code = "INSUFFICIENT_CREDITS"
    default_message = "You do not have enough credits for this task."

    def __init__(self, *, available: int, required: int) -> None:
        self.available = available
        self.required = required
        super().__init__(
            f"This task needs {required} credits and you have {available}.",
            details={"available": available, "required": required},
        )


class BillingUnavailable(AppError):
    """Payments could not be reached, or answered with a 5xx.

    A 503 rather than a 500: the fault is a dependency being down, the request
    was not wrong, and retrying later is the correct advice.
    """

    http_status = 503
    code = "BILLING_UNAVAILABLE"
    default_message = "Credits cannot be checked right now. Please try again shortly."


@dataclass(frozen=True, slots=True)
class Reservation:
    """A hold on somebody's wallet."""

    reservation_id: uuid.UUID
    amount: int


class BillingClient:
    """Talks to the payments service, or refuses to pretend it did.

    Takes an ``httpx.AsyncClient`` so a test can hand it a transport pointed at
    a fake payments app in the same process. Owning the client when none is
    given keeps the ordinary construction to one argument.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._owned = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.PAYMENTS_BASE_URL,
            timeout=settings.PAYMENTS_TIMEOUT_SECONDS,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    # ------------------------------------------------------------- the calls

    async def grant_signup(self, user_id: uuid.UUID) -> None:
        """Give a new account its signup credits.

        Idempotent per user by key, which is what makes it safe to deliver from
        an outbox that may retry after a response was lost.
        """
        await self._post(
            f"{INTERNAL_PREFIX}/grants/signup",
            payload={"userId": str(user_id)},
            idempotency_key=grant_key(user_id),
        )
        logger.info("billing_signup_granted", user_id=str(user_id))

    async def reserve(self, user_id: uuid.UUID, task_id: uuid.UUID, amount: int) -> Reservation:
        """Hold credits for a task that is about to start.

        The one call a user waits for. A 402 comes back as
        :class:`InsufficientCredits` with both numbers; anything else that fails
        comes back as :class:`BillingUnavailable`, because a task that cannot be
        paid for must not run, and a task whose payment is unknown must not
        either.
        """
        response = await self._post(
            f"{INTERNAL_PREFIX}/reservations",
            payload={"userId": str(user_id), "taskId": str(task_id), "amount": amount},
            idempotency_key=reserve_key(task_id),
            allow_status={402},
        )

        if response.status_code == 402:
            body = _json(response)
            raise InsufficientCredits(
                available=int(body.get("available", 0)),
                required=int(body.get("required", amount)),
            )

        body = _json(response)
        raw = body.get("reservationId")
        if not raw:
            # A 201 with no id is a broken contract, not a missing feature.
            logger.error("billing_reservation_without_id", task_id=str(task_id))
            raise BillingUnavailable
        try:
            reservation_id = uuid.UUID(str(raw))
        except ValueError as exc:
            logger.error("billing_reservation_id_malformed", task_id=str(task_id))
            raise BillingUnavailable from exc

        held = int(body.get("amount", amount))
        logger.info(
            "billing_reserved",
            task_id=str(task_id),
            reservation_id=str(reservation_id),
            amount=held,
        )
        return Reservation(reservation_id=reservation_id, amount=held)

    async def settle(self, reservation_id: uuid.UUID, actual: int) -> None:
        """Charge what the task actually used and release the rest."""
        await self._post(
            f"{INTERNAL_PREFIX}/reservations/{reservation_id}/settle",
            payload={"actualAmount": actual},
            idempotency_key=settle_key(reservation_id),
        )
        logger.info("billing_settled", reservation_id=str(reservation_id), amount=actual)

    async def release(self, reservation_id: uuid.UUID) -> None:
        """Give the whole hold back. For a task that did nothing at all."""
        await self._post(
            f"{INTERNAL_PREFIX}/reservations/{reservation_id}/release",
            payload={},
            idempotency_key=release_key(reservation_id),
        )
        logger.info("billing_released", reservation_id=str(reservation_id))

    # ---------------------------------------------------------------- plumbing

    async def _post(
        self,
        path: str,
        *,
        payload: dict[str, Any],
        idempotency_key: str,
        allow_status: frozenset[int] | set[int] = frozenset(),
    ) -> httpx.Response:
        """One call, retried only where retrying can help.

        A 5xx or a network failure is retried with a growing delay; a 4xx is
        returned or raised immediately, because the far side has decided and
        asking again wastes the user's time to reach the same answer.
        """
        attempts = max(1, self._settings.PAYMENTS_MAX_ATTEMPTS)
        last_status: int | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.post(
                    path,
                    json=payload,
                    headers=self._headers(idempotency_key),
                )
            except httpx.HTTPError as exc:
                # A timeout is the interesting one: the call may well have been
                # applied on the other side, which is exactly why every request
                # carries an idempotency key.
                logger.warning(
                    "billing_call_failed",
                    path=path,
                    attempt=attempt,
                    error_type=type(exc).__name__,
                )
                if attempt == attempts:
                    raise BillingUnavailable from exc
                await self._backoff(attempt)
                continue

            if response.status_code < 400 or response.status_code in allow_status:
                return response

            last_status = response.status_code
            if response.status_code not in RETRYABLE_STATUS:
                logger.warning("billing_call_refused", path=path, status=response.status_code)
                raise BillingUnavailable
            logger.warning(
                "billing_call_server_error", path=path, attempt=attempt, status=last_status
            )
            if attempt == attempts:
                break
            await self._backoff(attempt)

        raise BillingUnavailable

    def _headers(self, idempotency_key: str) -> dict[str, str]:
        token = self._settings.INTERNAL_API_TOKEN
        headers = {IDEMPOTENCY_HEADER: idempotency_key}
        if token is not None:
            headers[TOKEN_HEADER] = token.get_secret_value()
        return headers

    async def _backoff(self, attempt: int) -> None:
        """Exponential, from a tenth of the timeout.

        Short on purpose: ``reserve`` is on a request path, so the whole retry
        budget has to fit inside what a person will wait for. The outbox is
        where a long wait belongs.
        """
        await asyncio.sleep(self._settings.PAYMENTS_TIMEOUT_SECONDS * 0.1 * (2 ** (attempt - 1)))


def _json(response: httpx.Response) -> dict[str, Any]:
    """The body, or an empty mapping.

    A payments service that answers with something unparseable has failed, and
    the caller decides what that means: for a 402 it means the numbers are
    missing and the message is vaguer, not that the refusal is ignored.
    """
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
