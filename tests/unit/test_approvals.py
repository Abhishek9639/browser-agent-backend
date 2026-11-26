"""The approval gate and registry (spec section 4.10).

The database half is exercised against real rows in the integration suite;
these are about the waiting, the waking, and what each of the three outcomes
does to the run.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Any

from app.agent.planner.schema import Decision
from app.agent.safety.gate import DatabaseApprovalGate, describe_action
from app.domain.enums import ApprovalStatus, RiskLevel
from app.services.approvals import Answer, ApprovalRegistry
from app.services.events import EventType, InMemoryEventBus

TASK = uuid.UUID("aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa")


def decision(target: str = "Pay now") -> Decision:
    return Decision.model_validate(
        {
            "action": "click",
            "targetIndex": 0,
            "target": target,
            "reasoning": "complete the booking",
            "expectedOutcome": {"type": "url_contains", "value": "/pay"},
            "riskLevel": "HIGH",
            "confidence": 0.9,
        }
    )


class FakeApprovalRow:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.task_id = TASK
        self.approval_status = ApprovalStatus.PENDING
        self.reason: str | None = None
        self.decided_at: Any = None


class FakeApprovals:
    def __init__(self) -> None:
        self.rows: list[FakeApprovalRow] = []

    async def add(self, entity: Any) -> Any:
        row = FakeApprovalRow()
        row.reason = entity.reason
        self.rows.append(row)
        return row

    async def get_pending_for_task(self, task_id: uuid.UUID) -> FakeApprovalRow | None:
        return next((r for r in self.rows if r.approval_status is ApprovalStatus.PENDING), None)


class FakeUnitOfWork:
    def __init__(self, approvals: FakeApprovals) -> None:
        self.approvals = approvals


def unit_of_work_for(approvals: FakeApprovals) -> Any:
    @asynccontextmanager
    async def factory() -> Any:
        yield FakeUnitOfWork(approvals)

    return factory


def build(
    timeout: int = 30,
) -> tuple[DatabaseApprovalGate, ApprovalRegistry, InMemoryEventBus, FakeApprovals]:
    approvals = FakeApprovals()
    registry = ApprovalRegistry()
    bus = InMemoryEventBus()
    gate = DatabaseApprovalGate(unit_of_work_for(approvals), registry, bus, timeout_seconds=timeout)
    return gate, registry, bus, approvals


async def ask(gate: DatabaseApprovalGate, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "task_id": TASK,
        "step_number": 4,
        "decision": decision(),
        "risk": RiskLevel.HIGH,
        "screenshot": "base64==",
        "bbox": {"x": 1, "y": 2, "width": 3, "height": 4},
        "reasons": ["the control says 'pay now'"],
        "site": "SkyFare",
    }
    kwargs.update(overrides)
    return await gate.request(**kwargs)


# ------------------------------------------------------------- the prompt


def test_the_prompt_is_a_question_somebody_can_answer() -> None:
    sentence = describe_action(decision(), "SkyFare", ["the control says 'pay now'"])

    # "Approve step 6" is homework; this is a yes or no.
    assert sentence == ("Click 'Pay now' on SkyFare. Flagged because the control says 'pay now'.")


def test_the_prompt_survives_having_no_reasons() -> None:
    assert describe_action(decision(), "SkyFare", []) == "Click 'Pay now' on SkyFare."


# ------------------------------------------------------------- the waiting


async def test_an_approval_is_announced_with_everything_needed_to_answer_it() -> None:
    gate, registry, bus, _ = build()
    subscription = bus.subscribe(TASK)

    asking = asyncio.create_task(ask(gate))
    await asyncio.sleep(0.05)
    event = await subscription.__anext__()
    registry.resolve(_only_pending(registry), Answer(approved=True, by=uuid.uuid4()))
    await asking

    assert event.type is EventType.APPROVAL_REQUIRED
    assert event.step_number == 4
    assert event.risk_level is RiskLevel.HIGH
    assert event.screenshot == "base64=="
    assert event.bbox is not None and event.bbox.width == 3
    assert "Pay now" in (event.reason or "")
    assert event.proposed_action["action"] == "click"


async def test_the_row_is_written_before_the_event_goes_out() -> None:
    gate, registry, _, approvals = build()

    asking = asyncio.create_task(ask(gate))
    await asyncio.sleep(0.05)

    # The row is what survives a disconnect and a reconnect. The event is only
    # how a client finds out quickly.
    assert approvals.rows
    assert approvals.rows[0].approval_status is ApprovalStatus.PENDING
    registry.resolve(approvals.rows[0].id, Answer(approved=True))
    await asking


async def test_approving_lets_the_step_through() -> None:
    gate, registry, _, approvals = build()

    asking = asyncio.create_task(ask(gate))
    await asyncio.sleep(0.05)
    registry.resolve(approvals.rows[0].id, Answer(approved=True, reason="looks right"))
    outcome = await asking

    assert outcome.approved is True
    assert outcome.expired is False
    assert outcome.reason == "looks right"


async def test_rejecting_stops_the_step() -> None:
    gate, registry, _, approvals = build()

    asking = asyncio.create_task(ask(gate))
    await asyncio.sleep(0.05)
    registry.resolve(approvals.rows[0].id, Answer(approved=False, reason="too expensive"))
    outcome = await asking

    assert outcome.approved is False
    # Not the same as nobody answering: a person looked and said no.
    assert outcome.expired is False
    assert outcome.reason == "too expensive"


async def test_nobody_answering_expires_the_approval() -> None:
    gate, _, _, approvals = build(timeout=1)

    outcome = await ask(gate)

    assert outcome.approved is False
    assert outcome.expired is True
    assert outcome.reason == "approval timed out"
    assert approvals.rows[0].approval_status is ApprovalStatus.EXPIRED


async def test_the_registry_forgets_an_answered_approval() -> None:
    gate, registry, _, approvals = build()

    asking = asyncio.create_task(ask(gate))
    await asyncio.sleep(0.05)
    registry.resolve(approvals.rows[0].id, Answer(approved=True))
    await asking

    # Otherwise a long-lived process keeps an event and an answer per approval
    # it has ever waited on.
    assert registry.waiting == 0
    assert registry.answer_for(approvals.rows[0].id) is None


def test_resolving_something_nobody_waits_on_is_not_an_error() -> None:
    registry = ApprovalRegistry()

    # The run may have died, or be on another worker. The answer is in the
    # database either way.
    assert registry.resolve(uuid.uuid4(), Answer(approved=True)) is False


def _only_pending(registry: ApprovalRegistry) -> uuid.UUID:
    return next(iter(registry._waiting))


async def test_cancellation_wakes_an_unanswered_gate_and_expires_the_row() -> None:
    approvals = FakeApprovals()
    registry = ApprovalRegistry()
    bus = InMemoryEventBus()
    cancel = asyncio.Event()
    gate = DatabaseApprovalGate(
        unit_of_work_for(approvals), registry, bus, timeout_seconds=600, cancel_event=cancel
    )
    subscription = bus.subscribe(TASK)
    asking = asyncio.create_task(ask(gate))
    await asyncio.wait_for(subscription.__anext__(), timeout=1)
    cancel.set()
    outcome = await asyncio.wait_for(asking, timeout=1)
    assert not outcome.approved
    assert outcome.reason == "The task was cancelled."
    assert registry.waiting == 0
    assert approvals.rows[0].approval_status is ApprovalStatus.EXPIRED
    assert approvals.rows[0].decided_at is not None


async def test_cancellation_already_set_before_waiting_is_not_lost() -> None:
    approvals = FakeApprovals()
    registry = ApprovalRegistry()
    cancel = asyncio.Event()
    cancel.set()
    gate = DatabaseApprovalGate(
        unit_of_work_for(approvals), registry, InMemoryEventBus(), cancel_event=cancel
    )
    outcome = await asyncio.wait_for(ask(gate), timeout=1)
    assert not outcome.approved
    assert registry.waiting == 0
    assert approvals.rows[0].approval_status is ApprovalStatus.EXPIRED
