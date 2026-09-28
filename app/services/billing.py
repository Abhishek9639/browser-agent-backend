"""Billing, from this service's side (spec section 7).

Three things live here.

:class:`BillingService` is what the API and the agent talk to. It decides what
goes through the outbox and what does not, and it is the one place that checks
``BILLING_ENABLED``: everywhere else can call it unconditionally, which is what
makes the disabled path impossible to get wrong by forgetting a branch.

:class:`OutboxWorker` delivers what the outbox holds, retrying with a growing
delay.

The credit arithmetic is in :mod:`app.domain.credits`, because the stats
endpoint needs the same answer and a second copy would eventually disagree.
"""

import asyncio
import contextlib
import uuid
from typing import Final

from app.agent.orchestrator import UnitOfWorkFactory
from app.core.config import Settings
from app.core.logging import get_logger, task_context
from app.domain.credits import CreditPrices, Usage, credits_for_usage, reservation_for
from app.models.billing import (
    BillingOutbox,
    OutboxKind,
    grant_key,
    release_key,
    settle_key,
)
from app.repositories.unit_of_work import UnitOfWork
from app.services.billing_client import BillingClient, BillingUnavailable, Reservation

__all__ = ["BillingService", "OutboxWorker", "Usage"]

logger = get_logger(__name__)

# How many due rows one pass takes. Small, because a pass holds a transaction
# open for as long as the calls in it take.
BATCH_SIZE: Final = 20


class BillingService:
    """The only thing in this service that knows credits exist.

    Takes a unit of work rather than opening its own, so the outbox row it
    writes commits with the business change beside it. That is the whole design:
    a signup grant and its user row are one transaction, and a settle and its
    terminal status are one transaction.
    """

    def __init__(
        self,
        uow: UnitOfWork,
        settings: Settings,
        *,
        client: BillingClient | None = None,
    ) -> None:
        self._uow = uow
        self._settings = settings
        self._client = client

    @property
    def enabled(self) -> bool:
        return self._settings.BILLING_ENABLED

    # ----------------------------------------------------------- the grants

    async def queue_signup_grant(self, user_id: uuid.UUID) -> BillingOutbox | None:
        """Promise a new account its signup credits.

        Queued rather than called, because registration must not fail because
        payments is restarting: the account is created either way and the grant
        arrives when the service is back.
        """
        if not self.enabled:
            return None

        row = await self._uow.billing_outbox.enqueue(
            OutboxKind.GRANT_SIGNUP,
            {"userId": str(user_id)},
            idempotency_key=grant_key(user_id),
        )
        logger.info("billing_grant_queued", user_id=str(user_id), queued=row is not None)
        return row

    # ------------------------------------------------------ the reservation

    async def reserve_for_task(self, user_id: uuid.UUID, task_id: uuid.UUID) -> Reservation | None:
        """Hold credits before a task starts.

        The one synchronous call. A user submitting a task has to be told now
        whether they can afford it, so this raises
        :class:`~app.services.billing_client.InsufficientCredits` as a 402 and
        :class:`~app.services.billing_client.BillingUnavailable` as a 503
        rather than queueing anything.

        Returns ``None`` when billing is disabled, which is what makes every
        caller's happy path identical in both modes.
        """
        if not self.enabled:
            return None

        amount = reservation_for(self._settings.AGENT_MAX_STEPS, self._settings.RESERVE_MULTIPLIER)
        client = self._require_client()
        return await client.reserve(user_id, task_id, amount)

    # --------------------------------------------------------- the settling

    async def queue_settlement(
        self,
        task_id: uuid.UUID,
        reservation_id: uuid.UUID | None,
        usage: Usage,
    ) -> BillingOutbox | None:
        """Charge what a finished run actually used, or give the hold back.

        A run that took no steps is released rather than settled at zero. Both
        end with the wallet in the same place, but a release says "this task
        cost nothing" and a settle of zero says "this task was measured at
        nothing", and the ledger on the other side keeps them apart.
        """
        if not self.enabled or reservation_id is None:
            return None

        actual = self.credits_for_usage(usage)
        if actual <= 0:
            return await self._queue(
                OutboxKind.RELEASE,
                {"reservationId": str(reservation_id), "taskId": str(task_id)},
                idempotency_key=release_key(reservation_id),
            )

        return await self._queue(
            OutboxKind.SETTLE,
            {
                "reservationId": str(reservation_id),
                "taskId": str(task_id),
                "actualAmount": actual,
            },
            idempotency_key=settle_key(reservation_id),
        )

    def credits_for_usage(self, usage: Usage) -> int:
        """What this run is charged, at the configured prices."""
        return credits_for_usage(usage, CreditPrices.from_settings(self._settings))

    # ------------------------------------------------------------ internals

    async def _queue(
        self,
        kind: OutboxKind,
        payload: dict[str, object],
        *,
        idempotency_key: str,
    ) -> BillingOutbox | None:
        row = await self._uow.billing_outbox.enqueue(kind, payload, idempotency_key=idempotency_key)
        logger.info("billing_call_queued", kind=kind.value, queued=row is not None)
        return row

    def _require_client(self) -> BillingClient:
        if self._client is None:
            # Enabled with nothing to call is a deployment mistake, and a task
            # that runs unpaid because of it is worse than a 503.
            logger.error("billing_enabled_without_a_client")
            raise BillingUnavailable
        return self._client


class OutboxWorker:
    """Delivers the outbox, one batch at a time.

    Runs for the life of the process when billing is enabled. Every failure
    leaves the row where it is with a later ``next_attempt_at``, so nothing is
    lost by a crash: the backlog is in the database, not in this object.
    """

    def __init__(
        self,
        session_factory: UnitOfWorkFactory,
        settings: Settings,
        client: BillingClient,
    ) -> None:
        self._open = session_factory
        self._settings = settings
        self._client = client
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if not self._settings.BILLING_ENABLED or self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="billing-outbox")
        logger.info("billing_outbox_worker_started")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("billing_outbox_worker_stopped")

    async def _loop(self) -> None:
        while True:
            try:
                delivered = await self.drain()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A worker that dies takes the backlog with it until the next
                # restart, so nothing here may escape.
                logger.exception("billing_outbox_pass_failed", error_type=type(exc).__name__)
                delivered = 0

            if delivered == 0:
                await asyncio.sleep(self._settings.BILLING_OUTBOX_POLL_SECONDS)

    async def drain(self) -> int:
        """One pass. Returns how many calls were delivered.

        Each row is delivered inside the transaction that claimed it, so a row
        this worker is holding is invisible to the others and its result is
        recorded before the lock goes.
        """
        delivered = 0
        async with self._open() as uow:
            due = await uow.billing_outbox.claim_due(limit=BATCH_SIZE)
            for row in due:
                if await self._deliver(uow, row):
                    delivered += 1
        return delivered

    async def _deliver(self, uow: UnitOfWork, row: BillingOutbox) -> bool:
        payload = row.payload
        try:
            await self._call(row, payload)
        except Exception as exc:
            if row.attempts + 1 >= self._settings.BILLING_OUTBOX_MAX_ATTEMPTS:
                # Left undone on purpose rather than marked failed forever: the
                # row is the evidence that somebody's credits are unaccounted
                # for, and deleting it would hide that.
                logger.error(
                    "billing_outbox_exhausted",
                    kind=row.kind,
                    attempts=row.attempts + 1,
                    error_type=type(exc).__name__,
                )
            await uow.billing_outbox.mark_failed(
                row,
                error=type(exc).__name__,
                backoff_seconds=self._settings.BILLING_OUTBOX_BACKOFF_SECONDS,
            )
            return False

        await uow.billing_outbox.mark_done(row)
        logger.info("billing_outbox_delivered", kind=row.kind, attempts=row.attempts)
        return True

    async def _call(self, row: BillingOutbox, payload: dict[str, object]) -> None:
        kind = OutboxKind(row.kind)
        task_id = payload.get("taskId")
        with task_context(str(task_id)) if task_id else contextlib.nullcontext():
            match kind:
                case OutboxKind.GRANT_SIGNUP:
                    await self._client.grant_signup(uuid.UUID(str(payload["userId"])))
                case OutboxKind.SETTLE:
                    await self._client.settle(
                        uuid.UUID(str(payload["reservationId"])),
                        int(str(payload["actualAmount"])),
                    )
                case OutboxKind.RELEASE:
                    await self._client.release(uuid.UUID(str(payload["reservationId"])))
