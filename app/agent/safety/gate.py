"""The approval gate the loop waits on (spec section 4.10).

Writes the PENDING row, announces it, and blocks until somebody answers or the
clock runs out. The row is written and committed before the event goes out, so
a client that reconnects during the wait is told about the approval by the
replay rather than having missed its only announcement.
"""

import asyncio
import uuid

from app.agent.contracts import ApprovalOutcome
from app.agent.orchestrator import UnitOfWorkFactory
from app.agent.planner.schema import Decision
from app.core.logging import get_logger
from app.domain.enums import ActionType, ApprovalStatus, RiskLevel
from app.models.approval import Approval
from app.services.approvals import ApprovalRegistry, expire_pending
from app.services.events import ApprovalRequiredEvent, BoundingBox, EventBus

__all__ = ["DatabaseApprovalGate", "describe_action"]

logger = get_logger(__name__)


def describe_action(decision: Decision, site: str, reasons: list[str]) -> str:
    """One sentence a person can answer without reading the step log.

    "Click 'Pay now' on SkyFare" is a question somebody can say yes or no to.
    "Approve step 6" is a question they have to go and research, and an
    approval prompt nobody can answer quickly is one they will start waving
    through.
    """
    if decision.action is ActionType.WAIT and reasons:
        # A hand-off to the person (for example, signing in): the reason is the
        # whole question, and "Wait ... Flagged because" would bury it.
        return reasons[0]

    verb = decision.action.value.replace("_", " ")
    target = f" {decision.target!r}" if decision.target else ""
    where = f" on {site}" if site else ""
    sentence = f"{verb.capitalize()}{target}{where}".strip()

    if reasons:
        return f"{sentence}. Flagged because {reasons[0]}."
    return f"{sentence}."


class DatabaseApprovalGate:
    """Implements the `ApprovalGate` seam against the database."""

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        registry: ApprovalRegistry,
        events: EventBus,
        *,
        timeout_seconds: int = 600,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._registry = registry
        self._events = events
        self._timeout = timeout_seconds

    async def request(
        self,
        *,
        task_id: uuid.UUID,
        step_number: int,
        decision: Decision,
        risk: RiskLevel,
        screenshot: str | None,
        bbox: dict[str, int] | None,
        reasons: list[str],
        site: str,
    ) -> ApprovalOutcome:
        description = describe_action(decision, site, reasons)
        approval_id = await self._open(task_id, decision, risk, description)

        waiter = self._registry.wait_on(approval_id)
        await self._events.publish(
            task_id,
            ApprovalRequiredEvent(
                task_id=task_id,
                approval_id=approval_id,
                step_number=step_number,
                proposed_action=decision.model_dump(mode="json", by_alias=True),
                risk_level=risk,
                reason=description,
                screenshot=screenshot,
                bbox=BoundingBox(**bbox) if bbox else None,
            ),
        )
        logger.info(
            "approval_requested",
            task_id=str(task_id),
            approval_id=str(approval_id),
            step_number=step_number,
            reasons=reasons,
        )

        try:
            async with asyncio.timeout(self._timeout):
                await waiter.wait()
        except TimeoutError:
            return await self._expire(task_id, approval_id)
        finally:
            answer = self._registry.answer_for(approval_id)
            self._registry.forget(approval_id)

        if answer is None:  # pragma: no cover - the event only fires with an answer
            return await self._expire(task_id, approval_id)

        logger.info(
            "approval_answered",
            task_id=str(task_id),
            approved=answer.approved,
        )
        return ApprovalOutcome(
            approved=answer.approved,
            approval_id=approval_id,
            reason=answer.reason,
        )

    async def _open(
        self,
        task_id: uuid.UUID,
        decision: Decision,
        risk: RiskLevel,
        description: str,
    ) -> uuid.UUID:
        """Write the PENDING row, and commit it before announcing anything.

        The row is what survives a disconnect, a reconnect and a restart. The
        event is only how a client finds out quickly.
        """
        async with self._unit_of_work() as uow:
            approval = await uow.approvals.add(
                Approval(
                    task_id=task_id,
                    risk_flag=risk,
                    proposed_action=decision.model_dump(mode="json", by_alias=True),
                    approval_status=ApprovalStatus.PENDING,
                    reason=description,
                )
            )
            return approval.id

    async def _expire(self, task_id: uuid.UUID, approval_id: uuid.UUID) -> ApprovalOutcome:
        """Nobody answered in time.

        Spec section 4.3 and decision A4: the approval is EXPIRED and the task
        times out. REJECTED is reserved for a human actually saying no, which
        is a different thing to a human never looking.
        """
        async with self._unit_of_work() as uow:
            await expire_pending(uow, task_id, "approval timed out")

        logger.warning(
            "approval_timed_out",
            task_id=str(task_id),
            approval_id=str(approval_id),
            seconds=self._timeout,
        )
        return ApprovalOutcome(
            approved=False,
            approval_id=approval_id,
            reason="approval timed out",
            expired=True,
        )
