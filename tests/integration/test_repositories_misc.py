"""B05: user, refresh token, approval, cache and audit repositories."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ApprovalStatus, RiskLevel
from app.models.approval import Approval
from app.models.audit import AuditEvent
from app.repositories.approval import ApprovalRepository
from app.repositories.audit import AuditEventRepository
from app.repositories.cache import CachedFlowRepository
from app.repositories.user import RefreshTokenRepository, UserRepository
from tests.integration.conftest_helpers import (
    make_refresh_token,
    make_task,
    make_user,
)

# ------------------------------------------------------------------ users


async def test_get_by_email(db_session: AsyncSession) -> None:
    await make_user(db_session, "someone@example.test")

    found = await UserRepository(db_session).get_by_email("someone@example.test")

    assert found is not None
    assert found.email == "someone@example.test"


async def test_get_by_email_is_case_insensitive(db_session: AsyncSession) -> None:
    """Addresses are case-insensitive in practice; login must not care."""
    await make_user(db_session, "someone@example.test")

    found = await UserRepository(db_session).get_by_email("  SomeOne@Example.Test  ")

    assert found is not None


async def test_get_by_email_returns_none_when_absent(db_session: AsyncSession) -> None:
    assert await UserRepository(db_session).get_by_email("nobody@example.test") is None


async def test_email_exists(db_session: AsyncSession) -> None:
    repository = UserRepository(db_session)
    await make_user(db_session, "taken@example.test")

    assert await repository.email_exists("taken@example.test")
    assert not await repository.email_exists("free@example.test")


# ------------------------------------------------------------ refresh tokens


async def test_get_by_token_hash(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    await make_refresh_token(db_session, user, "hash-1")

    found = await RefreshTokenRepository(db_session).get_by_token_hash("hash-1")

    assert found is not None
    assert found.user_id == user.id


async def test_list_active_excludes_revoked_and_expired(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    repository = RefreshTokenRepository(db_session)
    live = await make_refresh_token(db_session, user, "live")
    revoked = await make_refresh_token(db_session, user, "revoked")
    expired = await make_refresh_token(db_session, user, "expired", expires_in_days=-1)

    await repository.revoke(revoked)

    active = await repository.list_active_for_user(user.id)

    assert [t.id for t in active] == [live.id]
    assert expired.id not in {t.id for t in active}


async def test_revoke_records_the_rotation_chain(db_session: AsyncSession) -> None:
    """replaced_by_id is what makes reuse of a rotated token detectable."""
    user = await make_user(db_session)
    repository = RefreshTokenRepository(db_session)
    old = await make_refresh_token(db_session, user, "old")
    new = await make_refresh_token(db_session, user, "new")

    await repository.revoke(old, replaced_by=new)

    assert old.revoked_at is not None
    assert old.replaced_by_id == new.id


async def test_revoke_all_for_user(db_session: AsyncSession) -> None:
    user = await make_user(db_session, "a@example.test")
    other = await make_user(db_session, "b@example.test")
    repository = RefreshTokenRepository(db_session)
    await make_refresh_token(db_session, user, "one")
    await make_refresh_token(db_session, user, "two")
    await make_refresh_token(db_session, other, "theirs")

    revoked = await repository.revoke_all_for_user(user.id)

    assert revoked == 2
    assert await repository.list_active_for_user(user.id) == []
    assert len(await repository.list_active_for_user(other.id)) == 1


# ------------------------------------------------------------------ approvals


async def test_get_pending_for_task(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    task = await make_task(db_session, user)
    repository = ApprovalRepository(db_session)
    await repository.add(
        Approval(task_id=task.id, risk_flag=RiskLevel.HIGH, proposed_action={"action": "click"})
    )

    pending = await repository.get_pending_for_task(task.id)

    assert pending is not None
    assert pending.approval_status is ApprovalStatus.PENDING


async def test_get_pending_ignores_decided_approvals(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    task = await make_task(db_session, user)
    repository = ApprovalRepository(db_session)
    await repository.add(
        Approval(
            task_id=task.id,
            risk_flag=RiskLevel.HIGH,
            proposed_action={},
            approval_status=ApprovalStatus.APPROVED,
        )
    )

    assert await repository.get_pending_for_task(task.id) is None


async def test_list_for_task_is_oldest_first(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    task = await make_task(db_session, user)
    repository = ApprovalRepository(db_session)
    first = await repository.add(
        Approval(task_id=task.id, risk_flag=RiskLevel.HIGH, proposed_action={"n": 1})
    )
    first.created_at = datetime.now(UTC) - timedelta(minutes=5)
    await db_session.flush()
    second = await repository.add(
        Approval(task_id=task.id, risk_flag=RiskLevel.MEDIUM, proposed_action={"n": 2})
    )

    approvals = await repository.list_for_task(task.id)

    assert [a.id for a in approvals] == [first.id, second.id]


async def test_list_expired_finds_stale_pending_approvals(db_session: AsyncSession) -> None:
    """Feeds the timeout job: EXPIRED drives the task to TIMED_OUT (A4)."""
    user = await make_user(db_session)
    task = await make_task(db_session, user)
    repository = ApprovalRepository(db_session)
    stale = await repository.add(
        Approval(task_id=task.id, risk_flag=RiskLevel.HIGH, proposed_action={})
    )
    stale.created_at = datetime.now(UTC) - timedelta(hours=2)
    await repository.add(Approval(task_id=task.id, risk_flag=RiskLevel.LOW, proposed_action={}))
    await db_session.flush()

    expired = await repository.list_expired(datetime.now(UTC) - timedelta(minutes=10))

    assert [a.id for a in expired] == [stale.id]


# ------------------------------------------------------------------ cache


async def test_get_by_signature(db_session: AsyncSession) -> None:
    repository = CachedFlowRepository(db_session)
    signature = "a" * 64
    await repository.upsert(signature, [{"action": "navigate"}])

    found = await repository.get_by_signature(signature)

    assert found is not None
    assert found.action_sequence == [{"action": "navigate"}]


async def test_upsert_inserts_then_updates(db_session: AsyncSession) -> None:
    repository = CachedFlowRepository(db_session)
    signature = "b" * 64

    first = await repository.upsert(signature, [{"action": "navigate"}])
    second = await repository.upsert(signature, [{"action": "click"}], success_rate=0.5)

    assert first.id == second.id, "upsert must not create a second row"
    assert second.action_sequence == [{"action": "click"}]
    assert second.success_rate == 0.5
    assert await repository.count() == 1


async def test_record_run_folds_into_the_success_rate(db_session: AsyncSession) -> None:
    repository = CachedFlowRepository(db_session)
    flow = await repository.upsert("c" * 64, [], success_rate=1.0, run_count=1)

    await repository.record_run(flow, succeeded=False)

    assert flow.run_count == 2
    assert flow.success_rate == 0.5


async def test_should_invalidate_needs_three_runs(db_session: AsyncSession) -> None:
    """Spec section 4.12: below 0.5 after at least three runs."""
    repository = CachedFlowRepository(db_session)
    flow = await repository.upsert("d" * 64, [], success_rate=0.0, run_count=2)

    assert not await repository.should_invalidate(flow)

    flow.run_count = 3
    assert await repository.should_invalidate(flow)


async def test_a_healthy_flow_is_not_invalidated(db_session: AsyncSession) -> None:
    repository = CachedFlowRepository(db_session)
    flow = await repository.upsert("e" * 64, [], success_rate=0.9, run_count=10)

    assert not await repository.should_invalidate(flow)


# ------------------------------------------------------------------ audit


async def test_audit_list_for_task_is_chronological(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    task = await make_task(db_session, user)
    repository = AuditEventRepository(db_session)
    first = await repository.add(AuditEvent(task_id=task.id, action="TASK_CREATED"))
    first.timestamp = datetime.now(UTC) - timedelta(minutes=5)
    await db_session.flush()
    second = await repository.add(AuditEvent(task_id=task.id, action="TASK_COMPLETED"))

    events = await repository.list_for_task(task.id)

    assert [e.id for e in events] == [first.id, second.id]


async def test_audit_list_for_actor_is_newest_first(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    repository = AuditEventRepository(db_session)
    old = await repository.add(AuditEvent(actor_id=user.id, action="USER_LOGGED_IN"))
    old.timestamp = datetime.now(UTC) - timedelta(days=1)
    await db_session.flush()
    recent = await repository.add(AuditEvent(actor_id=user.id, action="USER_LOGGED_OUT"))

    events = await repository.list_for_actor(user.id)

    assert [e.id for e in events] == [recent.id, old.id]
