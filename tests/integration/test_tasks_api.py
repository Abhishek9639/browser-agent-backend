"""B17: the task API end to end, and the runner against a real database.

The HTTP half runs against a fake runner: what these tests are about is the
rules, the envelope and the shapes the frontend generates from, and launching a
Chromium per case would add a second each to prove nothing extra. The runner's
own half, further down, uses the real TaskRunner against real Postgres, because
the thing worth testing there is exactly what ends up in the rows.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_screenshot_storage, get_task_runner
from app.core.config import Settings
from app.domain.enums import StepStatus, TaskStatus, UserRole
from app.models.task import Task, TaskStep
from app.repositories.unit_of_work import UnitOfWork
from app.services.storage import LocalFileStorage
from app.services.task_runner import TaskRunner, interrupted_reason
from tests.integration.conftest_helpers import make_task, make_user

ACCOUNT = {"name": "Abhishek", "email": "runner@example.test", "password": "a-long-passphrase"}
INSTRUCTION = "Find the cheapest flight from Delhi to Bengaluru on 20 Oct"


class FakeRunner:
    """Records what the API asked of it. Starts no browser."""

    def __init__(self) -> None:
        self.submitted: list[uuid.UUID] = []
        self.cancelled: list[uuid.UUID] = []

    def submit(self, task_id: uuid.UUID) -> None:
        self.submitted.append(task_id)

    def cancel_soon(self, task_id: uuid.UUID) -> bool:
        self.cancelled.append(task_id)
        return True

    async def cancel(self, task_id: uuid.UUID) -> bool:
        return self.cancel_soon(task_id)

    def is_running(self, task_id: uuid.UUID) -> bool:
        return task_id in self.submitted and task_id not in self.cancelled


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def tasks_app(
    api_app: FastAPI,
    runner: FakeRunner,
    settings: Settings,
    tmp_path: Path,
) -> FastAPI:
    storage = LocalFileStorage(settings.model_copy(update={"STORAGE_DIR": str(tmp_path)}))
    api_app.dependency_overrides[get_task_runner] = lambda: runner
    api_app.dependency_overrides[get_screenshot_storage] = lambda: storage
    api_app.state.screenshot_storage = storage
    return api_app


@pytest.fixture
async def client(tasks_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=tasks_app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def sign_in(client: AsyncClient, **overrides: Any) -> dict[str, str]:
    """Register and log in, returning the Authorization header."""
    body = {**ACCOUNT, **overrides}
    await client.post("/api/v1/auth/register", json=body)
    response = await client.post(
        "/api/v1/auth/login", json={"email": body["email"], "password": body["password"]}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['accessToken']}"}


@pytest.fixture
async def auth(client: AsyncClient) -> dict[str, str]:
    return await sign_in(client)


async def create(client: AsyncClient, auth: dict[str, str], **body: Any) -> Any:
    payload = {"instruction": INSTRUCTION, **body}
    return await client.post("/api/v1/tasks", json=payload, headers=auth)


# ----------------------------------------------------------------- TEST-001


async def test_a_valid_submission_is_accepted_and_queued(
    client: AsyncClient, auth: dict[str, str], runner: FakeRunner
) -> None:
    response = await create(client, auth)

    assert response.status_code == 201
    body = response.json()
    assert uuid.UUID(body["id"])
    assert body["status"] == TaskStatus.SUBMITTED.value
    assert body["instruction"] == INSTRUCTION
    assert body["taskNumber"].startswith("T-")
    assert body["createdAt"]
    assert body["finishedAt"] is None


async def test_each_submission_gets_its_own_id(client: AsyncClient, auth: dict[str, str]) -> None:
    first = await create(client, auth, instruction="Search for a flight to Delhi")
    second = await create(client, auth, instruction="Search for a hotel in Goa")

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["taskNumber"] != second.json()["taskNumber"]


async def test_the_runner_is_told_only_after_the_transaction_commits(
    client: AsyncClient, auth: dict[str, str], runner: FakeRunner
) -> None:
    response = await create(client, auth)

    # Scheduled as a background task, so by the time the response is out the
    # row the agent will read is committed rather than still in flight.
    assert runner.submitted == [uuid.UUID(response.json()["id"])]


async def test_an_instruction_is_stored_normalised(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    response = await create(client, auth, instruction="  Find   the\n cheapest   flight  ")

    assert response.json()["instruction"] == "Find the cheapest flight"


# ----------------------------------------------------------------- TEST-002


@pytest.mark.parametrize(
    ("instruction", "why"),
    [
        ("", "empty"),
        ("   ", "only whitespace"),
        ("\n\t ", "only whitespace of other kinds"),
        ("hi", "shorter than the minimum"),
        ("x" * 2001, "longer than the maximum"),
    ],
)
async def test_an_invalid_instruction_is_rejected_in_the_envelope(
    client: AsyncClient, auth: dict[str, str], instruction: str, why: str
) -> None:
    response = await create(client, auth, instruction=instruction)

    # 400, not 422: spec section 6 reserves 422 for an action that cannot be
    # carried out, and maps request validation to 400. The repo's handler does
    # that translation for every route, so this is the envelope a client sees.
    assert response.status_code == 400, why
    body = response.json()
    # Every error leaves in the same envelope, including the ones FastAPI
    # raises before a handler runs.
    assert set(body) == {"error"}
    assert body["error"]["code"]
    assert body["error"]["message"]
    assert body["error"]["requestId"]


async def test_a_missing_instruction_is_rejected(client: AsyncClient, auth: dict[str, str]) -> None:
    response = await client.post("/api/v1/tasks", json={}, headers=auth)

    assert response.status_code == 400
    assert response.json()["error"]["code"]


async def test_a_start_url_outside_the_allow_list_is_refused(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    response = await create(client, auth, startUrl="https://example.com/anything")

    assert response.status_code == 400
    assert "allowed domain" in response.json()["error"]["message"]


async def test_a_start_url_that_is_not_http_is_refused(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    response = await create(client, auth, startUrl="file:///etc/passwd")

    # Scheme is a schema rule, so it never reaches the service.
    assert response.status_code == 400


async def test_submitting_without_a_token_is_401(client: AsyncClient) -> None:
    response = await client.post("/api/v1/tasks", json={"instruction": INSTRUCTION})

    assert response.status_code == 401


# --------------------------------------------------------------- duplicates


async def test_the_same_live_instruction_twice_is_409_with_the_existing_id(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    first = await create(client, auth)
    second = await create(client, auth)

    assert second.status_code == 409
    # The client's next move is to open the task it already has, and it cannot
    # do that without the id.
    assert first.json()["id"] in second.json()["error"]["message"]


async def test_duplicate_detection_ignores_case_and_spacing(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    await create(client, auth, instruction="Find a flight to Delhi")
    again = await create(client, auth, instruction="  find   A FLIGHT to delhi ")

    assert again.status_code == 409


async def test_the_same_instruction_is_allowed_once_the_first_has_finished(
    client: AsyncClient, auth: dict[str, str], db_session: AsyncSession
) -> None:
    first = await create(client, auth)
    task = await db_session.get(Task, uuid.UUID(first.json()["id"]))
    assert task is not None
    task.status = TaskStatus.COMPLETED
    await db_session.commit()

    again = await create(client, auth)

    # Asking for the same thing tomorrow is a new task. Asking twice while the
    # first is still running is a double-submitted form.
    assert again.status_code == 201


async def test_another_user_may_submit_the_same_instruction(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    await create(client, auth)
    other = await sign_in(client, email="other@example.test")

    response = await create(client, other)

    assert response.status_code == 201


# ---------------------------------------------------------------- ownership


async def test_someone_elses_task_is_404_not_403(client: AsyncClient, auth: dict[str, str]) -> None:
    mine = await create(client, auth)
    other = await sign_in(client, email="stranger@example.test")

    response = await client.get(f"/api/v1/tasks/{mine.json()['id']}", headers=other)

    # 403 would confirm the id exists, which is itself a leak.
    assert response.status_code == 404


async def test_someone_elses_steps_are_404(client: AsyncClient, auth: dict[str, str]) -> None:
    mine = await create(client, auth)
    other = await sign_in(client, email="stranger2@example.test")

    response = await client.get(f"/api/v1/tasks/{mine.json()['id']}/steps", headers=other)

    assert response.status_code == 404


async def test_someone_elses_task_cannot_be_cancelled(
    client: AsyncClient, auth: dict[str, str], runner: FakeRunner
) -> None:
    mine = await create(client, auth)
    other = await sign_in(client, email="stranger3@example.test")

    response = await client.post(f"/api/v1/tasks/{mine.json()['id']}/cancel", headers=other)

    assert response.status_code == 404
    assert runner.cancelled == []


async def test_the_list_only_shows_your_own(client: AsyncClient, auth: dict[str, str]) -> None:
    await create(client, auth)
    other = await sign_in(client, email="stranger4@example.test")
    await create(client, other, instruction="Something else entirely")

    mine = await client.get("/api/v1/tasks", headers=auth)
    theirs = await client.get("/api/v1/tasks", headers=other)

    assert mine.json()["total"] == 1
    assert theirs.json()["total"] == 1
    assert mine.json()["items"][0]["id"] != theirs.json()["items"][0]["id"]


async def test_an_admin_sees_every_task(
    client: AsyncClient, auth: dict[str, str], db_session: AsyncSession
) -> None:
    await create(client, auth)
    admin_header = await sign_in(client, email="admin@example.test")
    admin = (
        await db_session.execute(select(__import__("app.models.user", fromlist=["User"]).User))
    ).scalars()
    for user in admin:
        if user.email == "admin@example.test":
            user.role = UserRole.ADMIN
    await db_session.commit()

    response = await client.get("/api/v1/tasks", headers=admin_header)

    assert response.json()["total"] == 1


# ------------------------------------------------------------------- cancel


async def test_cancelling_a_live_task_marks_it_and_signals_the_runner(
    client: AsyncClient, auth: dict[str, str], runner: FakeRunner
) -> None:
    created = await create(client, auth)
    task_id = created.json()["id"]

    response = await client.post(f"/api/v1/tasks/{task_id}/cancel", headers=auth)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == TaskStatus.CANCELLED.value
    assert body["finishedAt"] is not None
    assert body["failureReason"] == "Cancelled by the user."
    assert runner.cancelled == [uuid.UUID(task_id)]


async def test_cancelling_a_finished_task_is_409(client: AsyncClient, auth: dict[str, str]) -> None:
    created = await create(client, auth)
    task_id = created.json()["id"]
    await client.post(f"/api/v1/tasks/{task_id}/cancel", headers=auth)

    again = await client.post(f"/api/v1/tasks/{task_id}/cancel", headers=auth)

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "CONFLICT"


async def test_cancelling_frees_the_instruction_for_resubmission(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    created = await create(client, auth)
    await client.post(f"/api/v1/tasks/{created.json()['id']}/cancel", headers=auth)

    again = await create(client, auth)

    assert again.status_code == 201


# ------------------------------------------------------------ list and read


async def test_the_list_filters_by_status(client: AsyncClient, auth: dict[str, str]) -> None:
    first = await create(client, auth, instruction="Find a flight to Delhi")
    await create(client, auth, instruction="Find a hotel in Goa")
    await client.post(f"/api/v1/tasks/{first.json()['id']}/cancel", headers=auth)

    cancelled = await client.get("/api/v1/tasks?status=CANCELLED", headers=auth)
    live = await client.get("/api/v1/tasks?status=SUBMITTED", headers=auth)

    assert [t["id"] for t in cancelled.json()["items"]] == [first.json()["id"]]
    assert live.json()["total"] == 1


async def test_the_list_searches_the_instruction(client: AsyncClient, auth: dict[str, str]) -> None:
    await create(client, auth, instruction="Find a flight to Delhi")
    await create(client, auth, instruction="Find a hotel in Goa")

    response = await client.get("/api/v1/tasks?q=hotel", headers=auth)

    assert response.json()["total"] == 1
    assert "hotel" in response.json()["items"][0]["instruction"]


async def test_the_list_pages(client: AsyncClient, auth: dict[str, str]) -> None:
    for index in range(5):
        await create(client, auth, instruction=f"Find a flight number {index}")

    first = await client.get("/api/v1/tasks?page=1&pageSize=2", headers=auth)
    second = await client.get("/api/v1/tasks?page=2&pageSize=2", headers=auth)

    body = first.json()
    assert body["total"] == 5
    assert body["pageSize"] == 2
    assert body["totalPages"] == 3
    assert body["hasNext"] is True
    assert body["hasPrevious"] is False
    assert second.json()["hasPrevious"] is True
    assert {t["id"] for t in body["items"]}.isdisjoint({t["id"] for t in second.json()["items"]})


async def test_an_unknown_task_is_404(client: AsyncClient, auth: dict[str, str]) -> None:
    response = await client.get(f"/api/v1/tasks/{uuid.uuid4()}", headers=auth)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


# -------------------------------------------------------------------- steps


async def test_steps_come_back_in_order_with_their_detail(
    client: AsyncClient,
    auth: dict[str, str],
    db_session: AsyncSession,
    tasks_app: FastAPI,
    tmp_path: Path,
) -> None:
    created = await create(client, auth)
    task_id = uuid.UUID(created.json()["id"])

    for number in (1, 2):
        db_session.add(
            TaskStep(
                task_id=task_id,
                step_number=number,
                action="type",
                target="From",
                target_index=number,
                value_redacted="Delhi",
                status=StepStatus.VERIFIED,
            )
        )
    await db_session.commit()

    listed = await client.get(f"/api/v1/tasks/{task_id}/steps", headers=auth)
    steps = listed.json()

    assert [s["stepNumber"] for s in steps] == [1, 2]
    assert steps[0]["action"] == "type"
    assert steps[0]["valueRedacted"] == "Delhi"
    assert steps[0]["status"] == StepStatus.VERIFIED.value

    detail = await client.get(f"/api/v1/tasks/{task_id}/steps/{steps[0]['id']}", headers=auth)
    body = detail.json()
    assert body["stepNumber"] == 1
    # Present as keys even when empty, so the frontend's generated types match.
    assert body["llmDecision"] is None
    assert body["verification"] is None
    assert body["recoveryAttempts"] == []


async def test_a_step_id_from_another_task_is_404(
    client: AsyncClient, auth: dict[str, str], db_session: AsyncSession
) -> None:
    mine = await create(client, auth, instruction="Find a flight to Delhi")
    other = await create(client, auth, instruction="Find a hotel in Goa")
    step = TaskStep(task_id=uuid.UUID(other.json()["id"]), step_number=1, action="click")
    db_session.add(step)
    await db_session.commit()

    response = await client.get(f"/api/v1/tasks/{mine.json()['id']}/steps/{step.id}", headers=auth)

    # Ownership is checked on the task, and the step is then pinned to it.
    # Without the second half, a valid step id would read across tasks.
    assert response.status_code == 404


async def test_a_screenshot_is_served_to_the_owner_as_a_private_jpeg(
    client: AsyncClient,
    auth: dict[str, str],
    db_session: AsyncSession,
    tasks_app: FastAPI,
) -> None:
    created = await create(client, auth)
    task_id = uuid.UUID(created.json()["id"])
    storage = tasks_app.state.screenshot_storage
    reference = await storage.save(task_id, 1, b"\xff\xd8\xff-pretend-jpeg")

    step = TaskStep(task_id=task_id, step_number=1, action="click", screenshot_ref=reference)
    db_session.add(step)
    await db_session.commit()

    response = await client.get(f"/api/v1/tasks/{task_id}/steps/{step.id}/screenshot", headers=auth)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    # Spec section 10: a screenshot can show anything the page showed.
    assert "private" in response.headers["cache-control"]
    assert "no-store" in response.headers["cache-control"]
    assert response.content.startswith(b"\xff\xd8\xff")


async def test_a_screenshot_is_not_served_to_anyone_else(
    client: AsyncClient,
    auth: dict[str, str],
    db_session: AsyncSession,
    tasks_app: FastAPI,
) -> None:
    created = await create(client, auth)
    task_id = uuid.UUID(created.json()["id"])
    reference = await tasks_app.state.screenshot_storage.save(task_id, 1, b"\xff\xd8\xff-x")
    step = TaskStep(task_id=task_id, step_number=1, action="click", screenshot_ref=reference)
    db_session.add(step)
    await db_session.commit()
    other = await sign_in(client, email="nosy@example.test")

    response = await client.get(
        f"/api/v1/tasks/{task_id}/steps/{step.id}/screenshot", headers=other
    )

    assert response.status_code == 404


async def test_a_step_without_a_screenshot_is_404(
    client: AsyncClient, auth: dict[str, str], db_session: AsyncSession
) -> None:
    created = await create(client, auth)
    step = TaskStep(task_id=uuid.UUID(created.json()["id"]), step_number=1, action="click")
    db_session.add(step)
    await db_session.commit()

    response = await client.get(
        f"/api/v1/tasks/{created.json()['id']}/steps/{step.id}/screenshot", headers=auth
    )

    assert response.status_code == 404


# ------------------------------------------- the runner against real rows


def session_unit_of_work(session: AsyncSession) -> Any:
    @asynccontextmanager
    async def factory() -> AsyncIterator[UnitOfWork]:
        try:
            yield UnitOfWork(session)
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()

    return factory


class HeldOrchestrator:
    """Blocks until released, so a slot can be watched from outside."""

    def __init__(self, gate: Any, started: list[uuid.UUID]) -> None:
        self._gate = gate
        self._started = started

    async def run(self, task_id: uuid.UUID) -> Any:
        self._started.append(task_id)
        await self._gate.wait()
        from app.agent.orchestrator import RunOutcome

        return RunOutcome(status=TaskStatus.COMPLETED)


class NullDriver:
    async def launch(self) -> None: ...

    async def close(self) -> None: ...


async def test_the_concurrency_limit_leaves_extra_tasks_submitted(
    db_session: AsyncSession, settings: Settings
) -> None:
    import asyncio

    user = await make_user(db_session, "concurrency@example.test")
    tasks = [
        await make_task(db_session, user, instruction=f"Find flight {index}") for index in range(5)
    ]
    await db_session.commit()

    gate = asyncio.Event()
    started: list[uuid.UUID] = []
    runner = TaskRunner(
        settings.model_copy(update={"AGENT_MAX_CONCURRENT_TASKS": 2}),
        database=None,  # type: ignore[arg-type]
        unit_of_work=session_unit_of_work(db_session),
        driver_factory=NullDriver,  # type: ignore[arg-type]
        orchestrator_factory=lambda **kwargs: HeldOrchestrator(gate, started),
    )
    runner._started = True  # type: ignore[attr-defined]

    try:
        for task in tasks:
            runner.submit(task.id)
        for _ in range(10):
            await asyncio.sleep(0.005)

        assert len(started) == 2

        # The three that never started are still SUBMITTED in the database,
        # which is what the history screen shows while a server is busy.
        for task in tasks:
            await db_session.refresh(task)
        waiting = [task for task in tasks if task.id not in started]
        assert len(waiting) == 3
        assert all(task.status is TaskStatus.SUBMITTED for task in waiting)
    finally:
        gate.set()
        await asyncio.wait_for(
            asyncio.gather(*runner._tasks.values(), return_exceptions=True), timeout=10
        )


async def test_restart_recovery_finishes_tasks_the_last_process_abandoned(
    db_session: AsyncSession, settings: Settings
) -> None:
    user = await make_user(db_session, "recovery@example.test")
    acting = await make_task(db_session, user, instruction="A", status=TaskStatus.ACTING)
    verifying = await make_task(db_session, user, instruction="B", status=TaskStatus.VERIFYING)
    queued = await make_task(db_session, user, instruction="C", status=TaskStatus.SUBMITTED)
    waiting = await make_task(
        db_session, user, instruction="D", status=TaskStatus.AWAITING_APPROVAL
    )
    done = await make_task(db_session, user, instruction="E", status=TaskStatus.COMPLETED)
    await db_session.commit()

    runner = TaskRunner(
        settings,
        database=None,  # type: ignore[arg-type]
        unit_of_work=session_unit_of_work(db_session),
    )
    recovered = await runner._recover_interrupted()

    for task in (acting, verifying, queued, waiting, done):
        await db_session.refresh(task)

    assert recovered == 4
    # Nothing will ever move these again: the process that owned them is gone.
    assert acting.status is TaskStatus.EXECUTION_FAILED
    assert verifying.status is TaskStatus.EXECUTION_FAILED
    assert queued.status is TaskStatus.EXECUTION_FAILED
    # Nobody answered, because the process that would have asked them died.
    assert waiting.status is TaskStatus.TIMED_OUT
    assert done.status is TaskStatus.COMPLETED

    assert acting.failure_reason == interrupted_reason
    assert acting.finished_at is not None
    assert done.failure_reason is None
