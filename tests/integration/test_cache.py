"""The flow cache against a real database (spec section 4.12).

Two halves: the store's own arithmetic, which is what decides whether a flow
gets used or deleted, and the two endpoints a client uses to see and re-run a
flow. The replay loop itself is exercised against a real browser in the chaos
suite, because what it does is re-resolve locators on a live page.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.cache.flow import Locator, ReplayStep, encode_steps
from app.agent.cache.signature import task_signature
from app.agent.cache.store import DatabaseFlowCache
from app.api.deps import get_screenshot_storage, get_task_runner
from app.core.config import Settings
from app.domain.enums import ActionType, TaskStatus, VerificationMethod
from app.repositories.unit_of_work import UnitOfWork
from app.services.storage import LocalFileStorage
from tests.integration.conftest_helpers import make_task, make_user

pytestmark = pytest.mark.integration

INSTRUCTION = "Find the cheapest flight from Delhi to Bengaluru on 20 Oct"


def steps() -> tuple[ReplayStep, ...]:
    return (
        ReplayStep(
            action=ActionType.TYPE,
            locator=Locator(role="input", name="From", css_path="#from"),
            value="Delhi",
            target="From",
            expected_type=VerificationMethod.VALUE_EQUALS,
            expected_value="Delhi",
        ),
        ReplayStep(
            action=ActionType.CLICK,
            locator=Locator(role="button", name="Search flights", css_path="#search"),
            target="Search flights",
        ),
    )


def uow_factory(session: AsyncSession) -> Any:
    """A unit of work over the test transaction, which owns the commit."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[UnitOfWork]:
        yield UnitOfWork(session)

    return factory


# --------------------------------------------------------------- the store


async def test_a_completed_task_stores_a_flow_under_its_signature(
    db_session: AsyncSession,
) -> None:
    user = await make_user(db_session, email="cache-store@example.test")
    task = await make_task(db_session, user, INSTRUCTION, start_url="http://localhost:8200/")
    cache = DatabaseFlowCache(uow_factory(db_session))

    await cache.save(task.id, INSTRUCTION, steps())

    uow = UnitOfWork(db_session)
    flow = await uow.cached_flows.get_by_signature(
        task_signature(INSTRUCTION, "http://localhost:8200/")
    )
    assert flow is not None
    assert len(flow.action_sequence) == 2
    assert flow.run_count == 1
    assert flow.success_rate == 1.0
    await db_session.refresh(task)
    assert task.cache_flow_id == flow.id


async def test_a_second_task_with_the_same_instruction_gets_the_flow(
    db_session: AsyncSession,
) -> None:
    """TEST-009's first half: the signature is what makes a hit possible."""
    user = await make_user(db_session, email="cache-hit@example.test")
    first = await make_task(db_session, user, INSTRUCTION)
    second = await make_task(db_session, user, f"  {INSTRUCTION.upper()}  ")
    cache = DatabaseFlowCache(uow_factory(db_session))
    await cache.save(first.id, first.instruction, steps())

    plan = await cache.plan(second.id, second.instruction)

    assert plan is not None
    assert len(plan.steps) == 2
    assert plan.forced is False
    # Bound at lookup, not at the end: a run that dies mid-replay should still
    # be traceable to what it was replaying. Read from the session rather than
    # after a refresh, because the write belongs to the caller's transaction and
    # this one has not committed.
    await db_session.flush()
    assert second.cache_flow_id == plan.flow_id


async def test_a_flow_below_the_threshold_is_kept_and_not_used(
    db_session: AsyncSession,
) -> None:
    user = await make_user(db_session, email="cache-weak@example.test")
    task = await make_task(db_session, user, INSTRUCTION)
    uow = UnitOfWork(db_session)
    flow = await uow.cached_flows.upsert(
        task_signature(INSTRUCTION),
        encode_steps(steps()),
        success_rate=0.4,
        run_count=2,
    )
    cache = DatabaseFlowCache(uow_factory(db_session))

    assert await cache.plan(task.id, INSTRUCTION) is None
    # Not deleted yet: two runs is not enough to judge it (spec section 4.12).
    assert await uow.cached_flows.get(flow.id) is not None


async def test_a_task_bound_to_a_flow_replays_it_whatever_the_rate(
    db_session: AsyncSession,
) -> None:
    """What POST /replay does. The user asked for this sequence."""
    user = await make_user(db_session, email="cache-forced@example.test")
    uow = UnitOfWork(db_session)
    flow = await uow.cached_flows.upsert(
        task_signature(INSTRUCTION), encode_steps(steps()), success_rate=0.0, run_count=9
    )
    task = await make_task(db_session, user, INSTRUCTION, cache_flow_id=flow.id)
    cache = DatabaseFlowCache(uow_factory(db_session))

    plan = await cache.plan(task.id, INSTRUCTION)

    assert plan is not None
    assert plan.forced is True


async def test_a_failed_replay_lowers_the_success_rate(db_session: AsyncSession) -> None:
    user = await make_user(db_session, email="cache-lower@example.test")
    task = await make_task(db_session, user, INSTRUCTION)
    cache = DatabaseFlowCache(uow_factory(db_session))
    await cache.save(task.id, INSTRUCTION, steps())
    plan = await cache.plan(task.id, INSTRUCTION)
    assert plan is not None

    await cache.settle(plan, replayed_all=False)

    uow = UnitOfWork(db_session)
    flow = await uow.cached_flows.get(plan.flow_id)
    assert flow is not None
    assert flow.run_count == 2
    assert flow.success_rate == pytest.approx(0.5)


async def test_a_flow_that_keeps_failing_is_deleted(db_session: AsyncSession) -> None:
    """Below half after at least three runs, the row goes (spec section 4.12).

    Deleted rather than flagged: a flow nobody will replay is a flow that only
    costs a lookup, and keeping it would mean every task with this signature
    pays for the same dead answer forever.
    """
    user = await make_user(db_session, email="cache-invalid@example.test")
    task = await make_task(db_session, user, INSTRUCTION)
    uow = UnitOfWork(db_session)
    flow = await uow.cached_flows.upsert(
        task_signature(INSTRUCTION), encode_steps(steps()), success_rate=0.5, run_count=2
    )
    cache = DatabaseFlowCache(uow_factory(db_session))
    plan = await cache.plan(task.id, INSTRUCTION)
    assert plan is not None

    await cache.settle(plan, replayed_all=False)

    assert await uow.cached_flows.get(flow.id) is None


async def test_a_disabled_cache_neither_reads_nor_writes(db_session: AsyncSession) -> None:
    user = await make_user(db_session, email="cache-off@example.test")
    task = await make_task(db_session, user, INSTRUCTION)
    cache = DatabaseFlowCache(uow_factory(db_session), enabled=False)

    await cache.save(task.id, INSTRUCTION, steps())

    uow = UnitOfWork(db_session)
    assert await uow.cached_flows.get_by_signature(task_signature(INSTRUCTION)) is None
    assert await cache.plan(task.id, INSTRUCTION) is None


async def test_an_empty_sequence_is_not_stored(db_session: AsyncSession) -> None:
    """A task that completed without verifying anything has nothing to teach."""
    user = await make_user(db_session, email="cache-empty@example.test")
    task = await make_task(db_session, user, INSTRUCTION)
    cache = DatabaseFlowCache(uow_factory(db_session))

    await cache.save(task.id, INSTRUCTION, ())

    uow = UnitOfWork(db_session)
    assert await uow.cached_flows.get_by_signature(task_signature(INSTRUCTION)) is None


# ------------------------------------------------------------ the endpoints


class FakeRunner:
    def __init__(self) -> None:
        self.submitted: list[uuid.UUID] = []

    def submit(self, task_id: uuid.UUID) -> None:
        self.submitted.append(task_id)

    def cancel_soon(self, task_id: uuid.UUID) -> bool:
        return True

    async def cancel(self, task_id: uuid.UUID) -> bool:
        return True

    def is_running(self, task_id: uuid.UUID) -> bool:
        return False


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def cache_app(api_app: FastAPI, runner: FakeRunner, settings: Settings, tmp_path: Path) -> FastAPI:
    storage = LocalFileStorage(settings.model_copy(update={"STORAGE_DIR": str(tmp_path)}))
    api_app.dependency_overrides[get_task_runner] = lambda: runner
    api_app.dependency_overrides[get_screenshot_storage] = lambda: storage
    api_app.state.screenshot_storage = storage
    return api_app


@pytest.fixture
async def client(cache_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=cache_app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
async def auth(client: AsyncClient) -> dict[str, str]:
    body = {"name": "Abhishek", "email": "cache-api@example.test", "password": "a-long-passphrase"}
    await client.post("/api/v1/auth/register", json=body)
    response = await client.post(
        "/api/v1/auth/login", json={"email": body["email"], "password": body["password"]}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['accessToken']}"}


async def submit(client: AsyncClient, auth: dict[str, str]) -> Any:
    response = await client.post("/api/v1/tasks", json={"instruction": INSTRUCTION}, headers=auth)
    assert response.status_code == 201, response.text
    return response.json()


async def test_cache_match_says_no_when_nothing_is_stored(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    task = await submit(client, auth)

    response = await client.get(f"/api/v1/tasks/{task['id']}/cache-match", headers=auth)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["found"] is False
    assert body["eligible"] is False
    # The signature is returned either way: it is what the client needs to
    # understand why two tasks that look the same did not match.
    assert len(body["signature"]) == 64


async def test_cache_match_reports_the_flows_statistics(
    client: AsyncClient, auth: dict[str, str], db_session: AsyncSession
) -> None:
    task = await submit(client, auth)
    uow = UnitOfWork(db_session)
    await uow.cached_flows.upsert(
        task_signature(INSTRUCTION), encode_steps(steps()), success_rate=0.75, run_count=4
    )
    await db_session.commit()

    body = (await client.get(f"/api/v1/tasks/{task['id']}/cache-match", headers=auth)).json()

    assert body["found"] is True
    assert body["eligible"] is True
    assert body["stepCount"] == 2
    assert body["runCount"] == 4
    assert body["successRate"] == 0.75


async def test_a_weak_flow_is_reported_as_found_but_not_eligible(
    client: AsyncClient, auth: dict[str, str], db_session: AsyncSession
) -> None:
    task = await submit(client, auth)
    uow = UnitOfWork(db_session)
    await uow.cached_flows.upsert(
        task_signature(INSTRUCTION), encode_steps(steps()), success_rate=0.2, run_count=2
    )
    await db_session.commit()

    body = (await client.get(f"/api/v1/tasks/{task['id']}/cache-match", headers=auth)).json()

    assert body["found"] is True
    assert body["eligible"] is False


async def test_replay_creates_a_new_task_bound_to_the_flow(
    client: AsyncClient,
    auth: dict[str, str],
    db_session: AsyncSession,
    runner: FakeRunner,
) -> None:
    task = await submit(client, auth)
    uow = UnitOfWork(db_session)
    flow = await uow.cached_flows.upsert(
        task_signature(INSTRUCTION), encode_steps(steps()), success_rate=1.0, run_count=1
    )
    original = await uow.tasks.get_or_404(uuid.UUID(task["id"]))
    original.status = TaskStatus.COMPLETED
    await db_session.commit()

    response = await client.post(f"/api/v1/tasks/{task['id']}/replay", headers=auth)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] != task["id"]
    assert body["status"] == TaskStatus.SUBMITTED.value
    assert body["instruction"] == INSTRUCTION

    created = await uow.tasks.get_or_404(uuid.UUID(body["id"]))
    assert created.cache_flow_id == flow.id
    # Queued after the commit, like any other submission.
    assert uuid.UUID(body["id"]) in runner.submitted


async def test_replaying_a_running_task_is_a_conflict(
    client: AsyncClient, auth: dict[str, str], db_session: AsyncSession
) -> None:
    """The original is the record of a run. Re-running it in place would edit it."""
    task = await submit(client, auth)
    uow = UnitOfWork(db_session)
    await uow.cached_flows.upsert(
        task_signature(INSTRUCTION), encode_steps(steps()), success_rate=1.0, run_count=1
    )
    await db_session.commit()

    response = await client.post(f"/api/v1/tasks/{task['id']}/replay", headers=auth)

    assert response.status_code == 409, response.text


async def test_replaying_a_task_with_no_flow_is_a_404(
    client: AsyncClient, auth: dict[str, str], db_session: AsyncSession
) -> None:
    task = await submit(client, auth)
    uow = UnitOfWork(db_session)
    original = await uow.tasks.get_or_404(uuid.UUID(task["id"]))
    original.status = TaskStatus.COMPLETED
    await db_session.commit()

    response = await client.post(f"/api/v1/tasks/{task['id']}/replay", headers=auth)

    assert response.status_code == 404, response.text


async def test_someone_elses_task_cannot_be_inspected_or_replayed(
    client: AsyncClient, auth: dict[str, str], db_session: AsyncSession
) -> None:
    """404 rather than 403: a stranger must not learn the task exists."""
    other = await make_user(db_session, email="stranger@example.test")
    theirs = await make_task(db_session, other, INSTRUCTION)
    await db_session.commit()

    match = await client.get(f"/api/v1/tasks/{theirs.id}/cache-match", headers=auth)
    replay = await client.post(f"/api/v1/tasks/{theirs.id}/replay", headers=auth)

    assert match.status_code == 404
    assert replay.status_code == 404
