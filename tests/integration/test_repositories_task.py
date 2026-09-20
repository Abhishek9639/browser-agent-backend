"""B05: task repositories, ownership and filters."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.domain.enums import ActionType, TaskStatus, UserRole
from app.models.task import Task
from app.repositories.task import (
    RecoveryAttemptRepository,
    TaskRepository,
    TaskStepRepository,
)
from tests.integration.conftest_helpers import make_step, make_task, make_user

# ------------------------------------------------------------------ ownership


async def test_get_owned_returns_your_own_task(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    task = await make_task(db_session, user)

    found = await TaskRepository(db_session).get_owned(task.id, user)

    assert found.id == task.id


async def test_get_owned_hides_someone_elses_task(db_session: AsyncSession) -> None:
    """NotFound rather than Forbidden: 403 would confirm the id exists."""
    owner = await make_user(db_session, "owner@example.test")
    other = await make_user(db_session, "other@example.test")
    task = await make_task(db_session, owner)

    with pytest.raises(NotFound):
        await TaskRepository(db_session).get_owned(task.id, other)


async def test_forbidden_and_missing_are_indistinguishable(db_session: AsyncSession) -> None:
    """Same code and same message shape, so existence cannot be probed."""
    owner = await make_user(db_session, "owner@example.test")
    other = await make_user(db_session, "other@example.test")
    task = await make_task(db_session, owner)
    absent = uuid.uuid4()
    repository = TaskRepository(db_session)

    with pytest.raises(NotFound) as forbidden:
        await repository.get_owned(task.id, other)
    with pytest.raises(NotFound) as missing:
        await repository.get_owned(absent, other)

    assert forbidden.value.code == missing.value.code == "NOT_FOUND"
    assert forbidden.value.http_status == missing.value.http_status == 404
    # Only the id differs; the wording is identical.
    assert forbidden.value.message == f"Task {task.id} does not exist."
    assert missing.value.message == f"Task {absent} does not exist."


async def test_admin_can_open_anyones_task(db_session: AsyncSession) -> None:
    owner = await make_user(db_session, "owner@example.test")
    admin = await make_user(db_session, "admin@example.test", role=UserRole.ADMIN)
    task = await make_task(db_session, owner)

    found = await TaskRepository(db_session).get_owned(task.id, admin)

    assert found.id == task.id


async def test_get_owned_raises_for_a_missing_task(db_session: AsyncSession) -> None:
    user = await make_user(db_session)

    with pytest.raises(NotFound):
        await TaskRepository(db_session).get_owned(uuid.uuid4(), user)


# ------------------------------------------------------------------ filters


async def test_list_for_user_only_returns_your_tasks(db_session: AsyncSession) -> None:
    owner = await make_user(db_session, "owner@example.test")
    other = await make_user(db_session, "other@example.test")
    await make_task(db_session, owner, "mine")
    await make_task(db_session, other, "theirs")

    page = await TaskRepository(db_session).list_for_user(owner)

    assert [t.instruction for t in page.items] == ["mine"]
    assert page.total == 1


async def test_admin_sees_every_task(db_session: AsyncSession) -> None:
    owner = await make_user(db_session, "owner@example.test")
    admin = await make_user(db_session, "admin@example.test", role=UserRole.ADMIN)
    await make_task(db_session, owner, "theirs")
    await make_task(db_session, admin, "mine")

    page = await TaskRepository(db_session).list_for_user(admin)

    assert page.total == 2


async def test_filter_by_single_status(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    await make_task(db_session, user, "running")
    await make_task(db_session, user, "done", status=TaskStatus.COMPLETED)

    page = await TaskRepository(db_session).list_for_user(user, status=TaskStatus.COMPLETED)

    assert [t.instruction for t in page.items] == ["done"]


async def test_filter_by_several_statuses(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    await make_task(db_session, user, "a", status=TaskStatus.COMPLETED)
    await make_task(db_session, user, "b", status=TaskStatus.CANCELLED)
    await make_task(db_session, user, "c", status=TaskStatus.ACTING)

    page = await TaskRepository(db_session).list_for_user(
        user, status=[TaskStatus.COMPLETED, TaskStatus.CANCELLED]
    )

    assert page.total == 2


async def test_filter_by_date_range(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    old = await make_task(db_session, user, "old")
    recent = await make_task(db_session, user, "recent")

    old.created_at = datetime.now(UTC) - timedelta(days=10)
    await db_session.flush()

    page = await TaskRepository(db_session).list_for_user(
        user, created_from=datetime.now(UTC) - timedelta(days=1)
    )

    assert [t.id for t in page.items] == [recent.id]


async def test_text_search_on_instruction(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    await make_task(db_session, user, "find the cheapest flight to Bengaluru")
    await make_task(db_session, user, "book a hotel in Delhi")

    page = await TaskRepository(db_session).list_for_user(user, query="flight")

    assert page.total == 1


async def test_text_search_is_case_insensitive(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    await make_task(db_session, user, "Find The Cheapest Flight")

    page = await TaskRepository(db_session).list_for_user(user, query="cheapest")

    assert page.total == 1


async def test_text_search_escapes_wildcards(db_session: AsyncSession) -> None:
    """A search for `%` must not match every row."""
    user = await make_user(db_session)
    await make_task(db_session, user, "plain instruction")
    await make_task(db_session, user, "discount is 100% off")

    page = await TaskRepository(db_session).list_for_user(user, query="100%")

    assert page.total == 1
    assert "100%" in page.items[0].instruction


async def test_underscore_is_not_a_wildcard(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    await make_task(db_session, user, "task_one")
    await make_task(db_session, user, "taskXone")

    page = await TaskRepository(db_session).list_for_user(user, query="task_one")

    assert page.total == 1


async def test_filters_combine(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    await make_task(db_session, user, "flight search", status=TaskStatus.COMPLETED)
    await make_task(db_session, user, "flight search", status=TaskStatus.CANCELLED)
    await make_task(db_session, user, "hotel search", status=TaskStatus.COMPLETED)

    page = await TaskRepository(db_session).list_for_user(
        user, status=TaskStatus.COMPLETED, query="flight"
    )

    assert page.total == 1


async def test_blank_query_is_ignored(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    await make_task(db_session, user)

    page = await TaskRepository(db_session).list_for_user(user, query="   ")

    assert page.total == 1


async def test_newest_first(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    first = await make_task(db_session, user, "first")
    first.created_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.flush()
    second = await make_task(db_session, user, "second")

    page = await TaskRepository(db_session).list_for_user(user)

    assert [t.id for t in page.items] == [second.id, first.id]


# ------------------------------------------------------------------ misc task


async def test_get_by_number(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    task = await make_task(db_session, user)

    found = await TaskRepository(db_session).get_by_number(task.task_number)

    assert found is not None
    assert found.id == task.id


async def test_list_active_excludes_terminal_tasks(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    await make_task(db_session, user, "running", status=TaskStatus.ACTING)
    await make_task(db_session, user, "done", status=TaskStatus.COMPLETED)
    await make_task(db_session, user, "cancelled", status=TaskStatus.CANCELLED)

    active = await TaskRepository(db_session).list_active()

    assert [t.instruction for t in active] == ["running"]


async def test_count_active_for_user(db_session: AsyncSession) -> None:
    """Backs the per-user concurrency cap of 2 (decision A13)."""
    user = await make_user(db_session, "a@example.test")
    other = await make_user(db_session, "b@example.test")
    await make_task(db_session, user, "one", status=TaskStatus.ACTING)
    await make_task(db_session, user, "two", status=TaskStatus.PLANNING)
    await make_task(db_session, user, "done", status=TaskStatus.COMPLETED)
    await make_task(db_session, other, "theirs", status=TaskStatus.ACTING)

    assert await TaskRepository(db_session).count_active_for_user(user.id) == 2


# ------------------------------------------------------------------ steps


async def test_next_step_number_starts_at_one(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    task = await make_task(db_session, user)

    assert await TaskStepRepository(db_session).next_step_number(task.id) == 1


async def test_next_step_number_increments(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    task = await make_task(db_session, user)
    repository = TaskStepRepository(db_session)
    await make_step(db_session, task, 1)
    await make_step(db_session, task, 2)

    assert await repository.next_step_number(task.id) == 3


async def test_next_step_number_is_per_task(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    first = await make_task(db_session, user, "one")
    second = await make_task(db_session, user, "two")
    await make_step(db_session, first, 1)
    await make_step(db_session, first, 2)

    assert await TaskStepRepository(db_session).next_step_number(second.id) == 1


async def test_list_for_task_is_ordered_by_step_number(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    task = await make_task(db_session, user)
    await make_step(db_session, task, 3)
    await make_step(db_session, task, 1)
    await make_step(db_session, task, 2)

    steps = await TaskStepRepository(db_session).list_for_task(task.id)

    assert [s.step_number for s in steps] == [1, 2, 3]


async def test_list_after_supports_websocket_replay(db_session: AsyncSession) -> None:
    """Spec section 6.1: a reconnecting client asks for everything after N."""
    user = await make_user(db_session)
    task = await make_task(db_session, user)
    for number in range(1, 6):
        await make_step(db_session, task, number)

    missed = await TaskStepRepository(db_session).list_after(task.id, 3)

    assert [s.step_number for s in missed] == [4, 5]


async def test_list_after_the_last_step_is_empty(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    task = await make_task(db_session, user)
    await make_step(db_session, task, 1)

    assert await TaskStepRepository(db_session).list_after(task.id, 1) == []


async def test_get_step_by_number(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    task = await make_task(db_session, user)
    await make_step(db_session, task, 1, action=ActionType.NAVIGATE)

    step = await TaskStepRepository(db_session).get_by_number(task.id, 1)

    assert step is not None
    assert step.action is ActionType.NAVIGATE


# ------------------------------------------------------------------ recovery


async def test_recovery_attempts_for_a_task_span_its_steps(db_session: AsyncSession) -> None:
    from app.domain.enums import RecoveryOutcome
    from app.models.task import RecoveryAttempt

    user = await make_user(db_session)
    task = await make_task(db_session, user)
    first = await make_step(db_session, task, 1)
    second = await make_step(db_session, task, 2)
    repository = RecoveryAttemptRepository(db_session)

    await repository.add_all(
        [
            RecoveryAttempt(
                step_id=second.id,
                attempt_number=1,
                strategy_used="text_locator",
                outcome=RecoveryOutcome.FAILED,
            ),
            RecoveryAttempt(
                step_id=first.id,
                attempt_number=1,
                strategy_used="reresolve_by_index",
                outcome=RecoveryOutcome.SUCCEEDED,
            ),
        ]
    )

    attempts = await repository.list_for_task(task.id)

    assert [a.strategy_used for a in attempts] == ["reresolve_by_index", "text_locator"]


async def test_next_attempt_number(db_session: AsyncSession) -> None:
    from app.domain.enums import RecoveryOutcome
    from app.models.task import RecoveryAttempt

    user = await make_user(db_session)
    task = await make_task(db_session, user)
    step = await make_step(db_session, task, 1)
    repository = RecoveryAttemptRepository(db_session)

    assert await repository.next_attempt_number(step.id) == 1

    await repository.add(
        RecoveryAttempt(
            step_id=step.id,
            attempt_number=1,
            strategy_used="scroll_and_retry",
            outcome=RecoveryOutcome.FAILED,
        )
    )

    assert await repository.next_attempt_number(step.id) == 2


async def test_task_repository_list_uses_the_model(db_session: AsyncSession) -> None:
    """Sanity: the generic list() path works on a concrete repository."""
    user = await make_user(db_session)
    await make_task(db_session, user)

    page = await TaskRepository(db_session).list(order_by=[Task.created_at.desc()])

    assert page.total == 1
