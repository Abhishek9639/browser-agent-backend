"""B05: the generic repository and pagination maths."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.repositories.base import MAX_PAGE_SIZE, Page, escape_like
from app.repositories.task import TaskRepository
from app.repositories.user import UserRepository
from tests.integration.conftest_helpers import make_task, make_user


async def test_get_returns_none_for_a_missing_row(db_session: AsyncSession) -> None:
    assert await UserRepository(db_session).get(uuid.uuid4()) is None


async def test_get_or_404_raises_not_found(db_session: AsyncSession) -> None:
    missing = uuid.uuid4()

    with pytest.raises(NotFound) as caught:
        await UserRepository(db_session).get_or_404(missing)

    assert caught.value.http_status == 404
    assert caught.value.code == "NOT_FOUND"
    assert str(missing) in caught.value.message


async def test_add_flushes_so_server_defaults_are_readable(db_session: AsyncSession) -> None:
    """The caller needs the id and task_number immediately, without committing."""
    user = await make_user(db_session)
    repository = TaskRepository(db_session)

    from app.models.task import Task

    task = await repository.add(Task(instruction="go", created_by=user.id))

    assert task.id is not None
    assert task.task_number.startswith("T-")


async def test_add_does_not_commit(db_session: AsyncSession) -> None:
    """Repositories flush; the session owner commits. Proven by rolling back."""
    user = await make_user(db_session)
    repository = TaskRepository(db_session)

    from app.models.task import Task

    await repository.add(Task(instruction="doomed", created_by=user.id))
    assert await repository.count() == 1

    await db_session.rollback()

    assert await repository.count() == 0


# ------------------------------------------------------------------ paging


async def test_pagination_reports_the_full_total(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    for index in range(7):
        await make_task(db_session, user, f"task {index}")

    page = await TaskRepository(db_session).list_for_user(user, page=1, page_size=3)

    assert len(page.items) == 3
    assert page.total == 7
    assert page.page == 1
    assert page.page_size == 3


@pytest.mark.parametrize(
    ("total", "page_size", "expected_pages"),
    [(0, 20, 0), (1, 20, 1), (20, 20, 1), (21, 20, 2), (7, 3, 3), (100, 10, 10)],
)
def test_total_pages_maths(total: int, page_size: int, expected_pages: int) -> None:
    page: Page[object] = Page(items=[], total=total, page=1, page_size=page_size)

    assert page.total_pages == expected_pages


def test_has_next_and_previous() -> None:
    first: Page[object] = Page(items=[], total=25, page=1, page_size=10)
    middle: Page[object] = Page(items=[], total=25, page=2, page_size=10)
    last: Page[object] = Page(items=[], total=25, page=3, page_size=10)

    assert first.has_next and not first.has_previous
    assert middle.has_next and middle.has_previous
    assert not last.has_next and last.has_previous


async def test_the_last_page_holds_the_remainder(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    for index in range(7):
        await make_task(db_session, user, f"task {index}")

    page = await TaskRepository(db_session).list_for_user(user, page=3, page_size=3)

    assert len(page.items) == 1
    assert not page.has_next


async def test_a_page_past_the_end_is_empty_not_an_error(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    await make_task(db_session, user)

    page = await TaskRepository(db_session).list_for_user(user, page=99, page_size=20)

    assert page.items == []
    assert page.total == 1


async def test_page_size_is_capped(db_session: AsyncSession) -> None:
    """No caller gets to ask for a million rows."""
    user = await make_user(db_session)

    page = await TaskRepository(db_session).list_for_user(user, page_size=10_000)

    assert page.page_size == MAX_PAGE_SIZE


async def test_page_below_one_is_clamped(db_session: AsyncSession) -> None:
    user = await make_user(db_session)

    page = await TaskRepository(db_session).list_for_user(user, page=0)

    assert page.page == 1


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [("100%", r"100\%"), ("a_b", r"a\_b"), (r"back\slash", r"back\\slash"), ("plain", "plain")],
)
def test_escape_like(raw: str, escaped: str) -> None:
    assert escape_like(raw) == escaped
