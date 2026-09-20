"""B05: the audit service, and that its metadata is redacted before storage."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redaction import REDACTED
from app.repositories.audit import AuditEventRepository
from app.repositories.unit_of_work import UnitOfWork
from app.services.audit import AuditAction, AuditService
from tests.integration.conftest_helpers import make_task, make_user


def service(session: AsyncSession) -> AuditService:
    return AuditService(AuditEventRepository(session))


async def test_record_writes_an_event(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    task = await make_task(db_session, user)

    event = await service(db_session).record(
        AuditAction.TASK_CREATED,
        actor_id=user.id,
        task_id=task.id,
        metadata={"instruction": "find flights"},
    )

    assert event.action == "TASK_CREATED"
    assert event.actor_id == user.id
    assert event.task_id == task.id
    assert event.timestamp is not None


async def test_system_events_have_no_actor(db_session: AsyncSession) -> None:
    """The approval timeout job is not a person."""
    user = await make_user(db_session)
    task = await make_task(db_session, user)

    event = await service(db_session).record(AuditAction.APPROVAL_EXPIRED, task_id=task.id)

    assert event.actor_id is None


async def test_login_events_have_no_task(db_session: AsyncSession) -> None:
    user = await make_user(db_session)

    event = await service(db_session).record(AuditAction.USER_LOGGED_IN, actor_id=user.id)

    assert event.task_id is None


# ------------------------------------------------------------------ redaction


async def test_metadata_is_redacted_before_it_is_stored(db_session: AsyncSession) -> None:
    """Spec section 10: the value must never reach the table at all."""
    user = await make_user(db_session)

    await service(db_session).record(
        AuditAction.USER_LOGGED_IN,
        actor_id=user.id,
        metadata={"email": "a@example.test", "password": "hunter2", "token": "jwt-value"},
    )

    stored = await db_session.scalar(text("SELECT metadata::text FROM audit_events LIMIT 1"))

    assert "hunter2" not in str(stored)
    assert "jwt-value" not in str(stored)
    assert "a@example.test" in str(stored)


async def test_nested_metadata_is_redacted(db_session: AsyncSession) -> None:
    user = await make_user(db_session)

    event = await service(db_session).record(
        AuditAction.APPROVAL_GRANTED,
        actor_id=user.id,
        metadata={
            "proposed_action": {"action": "type", "value": "ok", "password": "secret-value"},
            "attempts": [{"api_key": "sk-leak"}, {"safe": 1}],
        },
    )

    assert event.event_metadata is not None
    assert event.event_metadata["proposed_action"]["password"] == REDACTED
    assert event.event_metadata["proposed_action"]["action"] == "type"
    assert event.event_metadata["attempts"][0]["api_key"] == REDACTED
    assert event.event_metadata["attempts"][1]["safe"] == 1


async def test_screenshots_are_never_persisted(db_session: AsyncSession) -> None:
    user = await make_user(db_session)

    event = await service(db_session).record(
        AuditAction.APPROVAL_REQUESTED,
        actor_id=user.id,
        metadata={"screenshot": "data:image/jpeg;base64,/9j/4AAQ", "step_number": 4},
    )

    assert event.event_metadata is not None
    assert event.event_metadata["screenshot"] == REDACTED
    assert event.event_metadata["step_number"] == 4


async def test_audit_and_logs_mask_identically(db_session: AsyncSession) -> None:
    """One rule, two consumers: app.core.redaction."""
    from app.core.logging import redact_sensitive

    payload = {"password": "p", "nested": {"authorization": "Bearer x"}, "ok": 1}
    user = await make_user(db_session)

    event = await service(db_session).record(
        AuditAction.USER_LOGGED_IN, actor_id=user.id, metadata=dict(payload)
    )
    logged = dict(redact_sensitive(None, "info", dict(payload)))  # type: ignore[arg-type]

    assert event.event_metadata == logged


async def test_no_metadata_stores_null(db_session: AsyncSession) -> None:
    user = await make_user(db_session)

    event = await service(db_session).record(AuditAction.USER_LOGGED_OUT, actor_id=user.id)

    assert event.event_metadata is None


async def test_the_caller_still_owns_the_transaction(db_session: AsyncSession) -> None:
    """The service writes; it does not commit."""
    user = await make_user(db_session)
    repository = AuditEventRepository(db_session)
    await AuditService(repository).record(AuditAction.USER_LOGGED_IN, actor_id=user.id)
    assert await repository.count() == 1

    await db_session.rollback()

    assert await repository.count() == 0


# ------------------------------------------------------------------ uow


async def test_unit_of_work_exposes_every_repository(db_session: AsyncSession) -> None:
    uow = UnitOfWork(db_session)

    assert uow.users.session is db_session
    assert uow.tasks.session is db_session
    assert uow.task_steps.session is db_session
    assert uow.llm_decisions.session is db_session
    assert uow.verifications.session is db_session
    assert uow.recovery_attempts.session is db_session
    assert uow.approvals.session is db_session
    assert uow.audit_events.session is db_session
    assert uow.cached_flows.session is db_session
    assert uow.refresh_tokens.session is db_session


async def test_repositories_are_cached(db_session: AsyncSession) -> None:
    uow = UnitOfWork(db_session)

    assert uow.tasks is uow.tasks


async def test_everything_in_one_unit_of_work_shares_a_transaction(
    db_session: AsyncSession,
) -> None:
    """A rollback undoes the whole unit, not part of it."""
    uow = UnitOfWork(db_session)
    user = await make_user(db_session)
    task = await make_task(db_session, user)
    await AuditService(uow.audit_events).record(AuditAction.TASK_CREATED, task_id=task.id)

    assert await uow.tasks.count() == 1
    assert await uow.audit_events.count() == 1

    await db_session.rollback()

    assert await uow.tasks.count() == 0
    assert await uow.audit_events.count() == 0


async def test_unit_of_work_has_no_commit() -> None:
    """Deliberate: a service must not be able to commit half a change."""
    assert not hasattr(UnitOfWork, "commit")
