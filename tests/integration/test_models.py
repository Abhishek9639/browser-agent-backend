"""B04: constraints and cascades, enforced by Postgres rather than by the ORM."""

import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    ActionType,
    ApprovalStatus,
    RecoveryOutcome,
    RiskLevel,
    StepStatus,
    TaskStatus,
    UserRole,
    VerificationMethod,
)
from app.models import (
    Approval,
    AuditEvent,
    CachedFlow,
    LLMDecision,
    RecoveryAttempt,
    RefreshToken,
    Task,
    TaskStep,
    User,
    Verification,
)


async def make_user(session: AsyncSession, email: str = "a@example.test") -> User:
    user = User(name="Test User", email=email, password_hash="argon2-digest")
    session.add(user)
    await session.flush()
    return user


async def make_task(session: AsyncSession, user: User, instruction: str = "find flights") -> Task:
    task = Task(instruction=instruction, created_by=user.id)
    session.add(task)
    await session.flush()
    await session.refresh(task)
    return task


# --------------------------------------------------------------- uniqueness


async def test_email_is_unique(db_session: AsyncSession) -> None:
    await make_user(db_session, "dup@example.test")
    db_session.add(User(name="Other", email="dup@example.test", password_hash="h"))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_task_number_is_unique(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    first = await make_task(db_session, user)

    db_session.add(Task(instruction="clash", created_by=user.id, task_number=first.task_number))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_step_number_is_unique_per_task(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    task = await make_task(db_session, user)
    db_session.add(TaskStep(task_id=task.id, step_number=1, action=ActionType.NAVIGATE))
    await db_session.flush()

    db_session.add(TaskStep(task_id=task.id, step_number=1, action=ActionType.CLICK))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_the_same_step_number_is_fine_on_a_different_task(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    first = await make_task(db_session, user, "one")
    second = await make_task(db_session, user, "two")

    db_session.add(TaskStep(task_id=first.id, step_number=1, action=ActionType.NAVIGATE))
    db_session.add(TaskStep(task_id=second.id, step_number=1, action=ActionType.NAVIGATE))

    await db_session.flush()  # must not raise


async def test_task_signature_is_unique(db_session: AsyncSession) -> None:
    signature = "a" * 64
    db_session.add(CachedFlow(task_signature=signature, action_sequence=[]))
    await db_session.flush()

    db_session.add(CachedFlow(task_signature=signature, action_sequence=[]))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_refresh_token_hash_is_unique(db_session: AsyncSession) -> None:
    from datetime import UTC, datetime, timedelta

    user = await make_user(db_session)
    expires = datetime.now(UTC) + timedelta(days=7)
    db_session.add(RefreshToken(user_id=user.id, token_hash="h1", expires_at=expires))
    await db_session.flush()

    db_session.add(RefreshToken(user_id=user.id, token_hash="h1", expires_at=expires))
    with pytest.raises(IntegrityError):
        await db_session.flush()


# --------------------------------------------------------------- task_number


async def test_task_number_format(db_session: AsyncSession) -> None:
    """Spec section 5 shows T-000123: six digits, zero padded."""
    import re

    user = await make_user(db_session)
    task = await make_task(db_session, user)

    assert re.fullmatch(r"T-\d{6}", task.task_number), task.task_number


async def test_task_numbers_come_from_a_sequence(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    first = await make_task(db_session, user, "one")
    second = await make_task(db_session, user, "two")

    assert first.task_number != second.task_number
    assert int(second.task_number[2:]) == int(first.task_number[2:]) + 1


# --------------------------------------------------------------- defaults


async def test_task_defaults(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    task = await make_task(db_session, user)

    assert task.status is TaskStatus.SUBMITTED
    assert isinstance(task.id, uuid.UUID)
    assert task.created_at.tzinfo is not None
    assert task.updated_at.tzinfo is not None
    assert task.finished_at is None


async def test_user_role_defaults_to_task_submitter(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    await db_session.refresh(user)

    assert user.role is UserRole.TASK_SUBMITTER


async def test_step_status_defaults_to_pending(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    task = await make_task(db_session, user)
    step = TaskStep(task_id=task.id, step_number=1, action=ActionType.CLICK)
    db_session.add(step)
    await db_session.flush()
    await db_session.refresh(step)

    assert step.status is StepStatus.PENDING


# --------------------------------------------------------------- enums in pg


async def test_enum_values_are_stored_not_member_names(db_session: AsyncSession) -> None:
    """`navigate`, not `NAVIGATE`: the DB holds the same spelling as the API."""
    user = await make_user(db_session)
    task = await make_task(db_session, user)
    db_session.add(TaskStep(task_id=task.id, step_number=1, action=ActionType.GO_BACK))
    await db_session.flush()

    stored = await db_session.scalar(text("SELECT action::text FROM task_steps LIMIT 1"))
    assert stored == "go_back"


async def test_postgres_rejects_a_value_outside_the_allow_list(db_session: AsyncSession) -> None:
    """Section 4.4 is enforced by the database, not only by application code."""
    user = await make_user(db_session)
    task = await make_task(db_session, user)

    with pytest.raises(DBAPIError):
        await db_session.execute(
            text(
                "INSERT INTO task_steps (task_id, step_number, action) "
                "VALUES (:tid, 1, 'sudo_rm_rf')"
            ),
            {"tid": task.id},
        )


# --------------------------------------------------------------- cascades


async def test_deleting_a_task_removes_its_whole_tree(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    task = await make_task(db_session, user)
    step = TaskStep(task_id=task.id, step_number=1, action=ActionType.CLICK)
    db_session.add(step)
    await db_session.flush()

    db_session.add_all(
        [
            LLMDecision(
                step_id=step.id,
                prompt_version="v1",
                model_name="mock",
                provider="mock",
                confidence=0.9,
                risk_level=RiskLevel.LOW,
            ),
            Verification(
                step_id=step.id,
                expected_outcome={"type": "url_contains", "value": "/results"},
                verified=True,
                verification_method=VerificationMethod.URL_CONTAINS,
            ),
            RecoveryAttempt(
                step_id=step.id,
                attempt_number=1,
                strategy_used="role_and_name_locator",
                outcome=RecoveryOutcome.SUCCEEDED,
            ),
            Approval(
                task_id=task.id,
                step_id=step.id,
                risk_flag=RiskLevel.HIGH,
                proposed_action={"action": "click"},
            ),
            AuditEvent(task_id=task.id, actor_id=user.id, action="TASK_CREATED"),
        ]
    )
    await db_session.flush()

    await db_session.execute(text("DELETE FROM tasks WHERE id = :tid"), {"tid": task.id})
    await db_session.flush()

    for model in (TaskStep, LLMDecision, Verification, RecoveryAttempt, Approval, AuditEvent):
        remaining = await db_session.scalar(select(func.count()).select_from(model))
        assert remaining == 0, model.__name__


async def test_a_user_with_tasks_cannot_be_deleted(db_session: AsyncSession) -> None:
    """ON DELETE RESTRICT: history is not silently destroyed."""
    user = await make_user(db_session)
    await make_task(db_session, user)

    with pytest.raises(IntegrityError):
        await db_session.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": user.id})
        await db_session.flush()


async def test_deleting_a_cached_flow_nulls_the_task_reference(db_session: AsyncSession) -> None:
    """Invalidating a cache entry must not delete the tasks that used it."""
    user = await make_user(db_session)
    flow = CachedFlow(task_signature="b" * 64, action_sequence=[])
    db_session.add(flow)
    await db_session.flush()

    task = Task(instruction="cached", created_by=user.id, cache_flow_id=flow.id)
    db_session.add(task)
    await db_session.flush()

    await db_session.execute(text("DELETE FROM cached_flows WHERE id = :fid"), {"fid": flow.id})
    await db_session.flush()
    await db_session.refresh(task)

    assert task.cache_flow_id is None


async def test_deleting_a_user_removes_their_refresh_tokens(db_session: AsyncSession) -> None:
    from datetime import UTC, datetime, timedelta

    user = await make_user(db_session)
    db_session.add(
        RefreshToken(
            user_id=user.id,
            token_hash="to-be-removed",
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
    )
    await db_session.flush()

    await db_session.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": user.id})
    await db_session.flush()

    assert await db_session.scalar(select(func.count()).select_from(RefreshToken)) == 0


# --------------------------------------------------------------- misc


async def test_audit_metadata_round_trips_through_the_renamed_attribute(
    db_session: AsyncSession,
) -> None:
    event = AuditEvent(action="APPROVAL_DECIDED", event_metadata={"approved": True})
    db_session.add(event)
    await db_session.flush()
    await db_session.refresh(event)

    assert event.event_metadata == {"approved": True}
    stored = await db_session.scalar(text("SELECT metadata->>'approved' FROM audit_events"))
    assert stored == "true"


async def test_relationships_raise_instead_of_lazy_loading(db_session: AsyncSession) -> None:
    """lazy="raise" turns an accidental N+1 into an immediate, obvious error."""
    from sqlalchemy.exc import InvalidRequestError

    user = await make_user(db_session)
    task = await make_task(db_session, user)
    db_session.expire(task)
    await db_session.refresh(task)

    with pytest.raises(InvalidRequestError):
        _ = task.steps


async def test_approval_status_defaults_to_pending(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    task = await make_task(db_session, user)
    approval = Approval(task_id=task.id, risk_flag=RiskLevel.HIGH, proposed_action={"a": 1})
    db_session.add(approval)
    await db_session.flush()
    await db_session.refresh(approval)

    assert approval.approval_status is ApprovalStatus.PENDING
    assert approval.step_id is None  # may precede the step row
