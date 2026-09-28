"""Metrics and the stats endpoint against a real database (spec sections 6, 9).

Three things worth an integration test. The metrics endpoint's authorisation,
because it publishes how much everything costs and who is failing. The counters
moving when a task runs, because an instrument nobody verified is decoration.
And the stats arithmetic, because every rate in it is a division that can be
wrong in a way nobody notices until a reviewer asks where the number came from.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_screenshot_storage, get_task_runner
from app.core import metrics
from app.core.config import Settings
from app.domain.enums import ActionType, RecoveryOutcome, StepStatus, TaskStatus, UserRole
from app.models.approval import Approval
from app.models.task import LLMDecision, RecoveryAttempt, TaskStep
from app.repositories.unit_of_work import UnitOfWork
from app.services.storage import LocalFileStorage
from tests.integration.conftest_helpers import make_task, make_user

pytestmark = pytest.mark.integration

INSTRUCTION = "Find the cheapest flight from Delhi to Bengaluru on 20 Oct"
SCRAPE_TOKEN = "a-long-internal-scrape-token"


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
def ops_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"METRICS_INTERNAL_TOKEN": SecretStr(SCRAPE_TOKEN)})


@pytest.fixture
def ops_app(api_app: FastAPI, settings: Settings, tmp_path: Path) -> FastAPI:
    storage = LocalFileStorage(settings.model_copy(update={"STORAGE_DIR": str(tmp_path)}))
    api_app.dependency_overrides[get_task_runner] = lambda: FakeRunner()
    api_app.dependency_overrides[get_screenshot_storage] = lambda: storage
    api_app.state.screenshot_storage = storage
    api_app.state.settings = api_app.state.settings.model_copy(
        update={"METRICS_INTERNAL_TOKEN": SecretStr(SCRAPE_TOKEN)}
    )
    return api_app


@pytest.fixture
async def client(ops_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=ops_app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def sign_in(
    client: AsyncClient,
    db_session: AsyncSession,
    *,
    email: str,
    role: UserRole = UserRole.TASK_SUBMITTER,
) -> dict[str, str]:
    """Register, promote if asked, and return the Authorization header."""
    body = {"name": "Abhishek", "email": email, "password": "a-long-passphrase"}
    await client.post("/api/v1/auth/register", json=body)

    if role is not UserRole.TASK_SUBMITTER:
        uow = UnitOfWork(db_session)
        user = await uow.users.get_by_email(email)
        assert user is not None
        user.role = role
        await db_session.commit()

    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": body["password"]}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['accessToken']}"}


# ------------------------------------------------------------------ /metrics


async def test_metrics_refuses_an_anonymous_caller(client: AsyncClient) -> None:
    response = await client.get("/metrics")

    assert response.status_code == 401


async def test_metrics_refuses_an_ordinary_user(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """403, not 404.

    The endpoint's existence is not a secret; its contents are. By this point
    the caller has proved who they are, so telling them they lack the role is
    more useful than pretending the path is not there.
    """
    auth = await sign_in(client, db_session, email="plain@example.test")

    response = await client.get("/metrics", headers=auth)

    assert response.status_code == 403


async def test_metrics_accepts_an_admin(client: AsyncClient, db_session: AsyncSession) -> None:
    auth = await sign_in(client, db_session, email="admin@example.test", role=UserRole.ADMIN)

    response = await client.get("/metrics", headers=auth)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain")
    assert "# TYPE tasks_total counter" in response.text


async def test_metrics_accepts_the_internal_token(client: AsyncClient) -> None:
    """A scraper has no account and cannot obtain one."""
    response = await client.get("/metrics", headers={"X-Internal-Token": SCRAPE_TOKEN})

    assert response.status_code == 200, response.text


async def test_metrics_refuses_the_wrong_internal_token(client: AsyncClient) -> None:
    response = await client.get("/metrics", headers={"X-Internal-Token": "not-the-token"})

    assert response.status_code == 401


async def test_an_api_request_moves_the_http_histogram(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Labelled by the route template, never by the path.

    A metric labelled with the task id in the URL grows one time series per
    task, which is how instrumentation takes a service down.
    """
    metrics.reset()
    auth = await sign_in(client, db_session, email="counting@example.test", role=UserRole.ADMIN)
    task = await client.post("/api/v1/tasks", json={"instruction": INSTRUCTION}, headers=auth)
    task_id = task.json()["id"]
    await client.get(f"/api/v1/tasks/{task_id}", headers=auth)

    body = (await client.get("/metrics", headers=auth)).text

    assert '/api/v1/tasks/{task_id}"' in body
    assert task_id not in body


async def test_a_failed_request_is_counted_as_an_error(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    metrics.reset()
    auth = await sign_in(client, db_session, email="errors@example.test", role=UserRole.ADMIN)
    await client.get(f"/api/v1/tasks/{uuid.uuid4()}", headers=auth)

    body = (await client.get("/metrics", headers=auth)).text

    assert "http_errors_total" in body
    assert 'status="404"' in body


# ------------------------------------------------------------ /stats/overview


async def finished(
    session: AsyncSession,
    user: Any,
    *,
    status: TaskStatus,
    seconds: int,
    days_ago: int = 0,
) -> Any:
    """A task that has already ended, with a duration worth measuring."""
    created = datetime.now(UTC) - timedelta(days=days_ago, seconds=seconds)
    task = await make_task(session, user, INSTRUCTION)
    task.status = status
    task.created_at = created
    task.finished_at = created + timedelta(seconds=seconds)
    await session.flush()
    return task


async def test_the_overview_is_empty_rather_than_zero_for_a_new_user(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A rate of zero claims that nothing succeeded. Null says there is nothing."""
    auth = await sign_in(client, db_session, email="fresh@example.test")

    body = (await client.get("/api/v1/stats/overview", headers=auth)).json()

    assert body["tasksTotal"] == 0
    assert body["completionRate"] is None
    assert body["medianDurationSeconds"] is None
    assert body["recoverySuccessRate"] is None
    assert body["cacheHitRate"] is None


async def test_the_completion_rate_and_median_are_computed_over_finished_tasks(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Three of four completed, and the median of 10, 20, 30, 40."""
    auth = await sign_in(client, db_session, email="rates@example.test")
    uow = UnitOfWork(db_session)
    user = await uow.users.get_by_email("rates@example.test")
    assert user is not None

    await finished(db_session, user, status=TaskStatus.COMPLETED, seconds=10)
    await finished(db_session, user, status=TaskStatus.COMPLETED, seconds=20)
    await finished(db_session, user, status=TaskStatus.COMPLETED, seconds=30)
    await finished(db_session, user, status=TaskStatus.EXECUTION_FAILED, seconds=40)
    # Still running, so it counts towards nothing: a task in flight is not a
    # failure, and including it would dip the rate on every submission.
    await make_task(db_session, user, INSTRUCTION)
    await db_session.commit()

    body = (await client.get("/api/v1/stats/overview", headers=auth)).json()

    assert body["tasksTotal"] == 4
    assert body["tasksCompleted"] == 3
    assert body["completionRate"] == 0.75
    assert body["medianDurationSeconds"] == pytest.approx(25.0, abs=0.5)


async def test_the_recovery_rate_counts_steps_not_attempts(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Two steps needed rescuing, one was rescued, across five attempts.

    Counting attempts would report one success in five. The section 9 rate is
    per induced failure, and getting it wrong once turned 80% into 36%.
    """
    auth = await sign_in(client, db_session, email="recovery@example.test")
    uow = UnitOfWork(db_session)
    user = await uow.users.get_by_email("recovery@example.test")
    assert user is not None
    task = await finished(db_session, user, status=TaskStatus.COMPLETED, seconds=30)

    rescued = TaskStep(
        task_id=task.id, step_number=1, action=ActionType.CLICK, status=StepStatus.VERIFIED
    )
    lost = TaskStep(
        task_id=task.id, step_number=2, action=ActionType.CLICK, status=StepStatus.FAILED
    )
    db_session.add_all([rescued, lost])
    await db_session.flush()
    db_session.add_all(
        [
            RecoveryAttempt(
                step_id=rescued.id,
                attempt_number=n,
                strategy_used="reperceive_by_index",
                outcome=RecoveryOutcome.SUCCEEDED if n == 2 else RecoveryOutcome.FAILED,
            )
            for n in (1, 2)
        ]
        + [
            RecoveryAttempt(
                step_id=lost.id,
                attempt_number=n,
                strategy_used="text_locator",
                outcome=RecoveryOutcome.FAILED,
            )
            for n in (1, 2, 3)
        ]
    )
    await db_session.commit()

    body = (await client.get("/api/v1/stats/overview", headers=auth)).json()

    assert body["recoveryEpisodes"] == 2
    assert body["recoverySuccessRate"] == 0.5


async def test_the_cache_rate_counts_tasks_that_replayed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A replayed step is one with no ``llm_decisions`` row.

    Not `cache_flow_id`, which is set both when a task replays a flow and when
    it stores one, and would report every completed task as a hit.
    """
    auth = await sign_in(client, db_session, email="cacherate@example.test")
    uow = UnitOfWork(db_session)
    user = await uow.users.get_by_email("cacherate@example.test")
    assert user is not None

    planned_task = await finished(db_session, user, status=TaskStatus.COMPLETED, seconds=10)
    replayed_task = await finished(db_session, user, status=TaskStatus.COMPLETED, seconds=10)

    planned_step = TaskStep(
        task_id=planned_task.id, step_number=1, action=ActionType.CLICK, status=StepStatus.VERIFIED
    )
    replayed_step = TaskStep(
        task_id=replayed_task.id,
        step_number=1,
        action=ActionType.CLICK,
        status=StepStatus.VERIFIED,
    )
    db_session.add_all([planned_step, replayed_step])
    await db_session.flush()
    db_session.add(
        LLMDecision(
            step_id=planned_step.id,
            prompt_version="v2",
            model_name="mock-1",
            provider="mock",
            confidence=0.9,
            risk_level="LOW",
        )
    )
    await db_session.commit()

    body = (await client.get("/api/v1/stats/overview", headers=auth)).json()

    assert body["cacheHitRate"] == 0.5


async def test_approvals_are_counted(client: AsyncClient, db_session: AsyncSession) -> None:
    auth = await sign_in(client, db_session, email="approvals@example.test")
    uow = UnitOfWork(db_session)
    user = await uow.users.get_by_email("approvals@example.test")
    assert user is not None
    task = await finished(db_session, user, status=TaskStatus.COMPLETED, seconds=10)
    db_session.add(Approval(task_id=task.id, proposed_action={"action": "click"}, risk_flag="HIGH"))
    await db_session.commit()

    body = (await client.get("/api/v1/stats/overview", headers=auth)).json()

    assert body["approvalsRequested"] == 1


async def test_credits_are_reported_only_when_billing_is_enabled(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The ledger lives in the payments service; this is what we can see."""
    auth = await sign_in(client, db_session, email="credits@example.test")

    body = (await client.get("/api/v1/stats/overview", headers=auth)).json()

    assert body["creditsUsed"] is None


async def test_the_trend_covers_a_week_by_day(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    auth = await sign_in(client, db_session, email="trend@example.test")
    uow = UnitOfWork(db_session)
    user = await uow.users.get_by_email("trend@example.test")
    assert user is not None
    await finished(db_session, user, status=TaskStatus.COMPLETED, seconds=10, days_ago=0)
    await finished(db_session, user, status=TaskStatus.COMPLETED, seconds=10, days_ago=2)
    # Older than the window, so it must not appear.
    await finished(db_session, user, status=TaskStatus.COMPLETED, seconds=10, days_ago=30)
    await db_session.commit()

    body = (await client.get("/api/v1/stats/overview", headers=auth)).json()

    assert len(body["trend"]) == 2
    assert all(point["completed"] == 1 for point in body["trend"])


async def test_one_user_never_sees_anothers_numbers(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    mine = await sign_in(client, db_session, email="mine@example.test")
    uow = UnitOfWork(db_session)
    stranger = await make_user(db_session, email="stranger-stats@example.test")
    await finished(db_session, stranger, status=TaskStatus.COMPLETED, seconds=10)
    await db_session.commit()

    body = (await client.get("/api/v1/stats/overview", headers=mine)).json()

    assert body["scope"] == "mine"
    assert body["tasksTotal"] == 0
    assert uow is not None


async def test_only_an_admin_can_ask_for_the_global_scope(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """403 rather than silently narrowing.

    A dashboard showing one user's numbers under a global heading would be
    worse than an error.
    """
    auth = await sign_in(client, db_session, email="notadmin@example.test")

    response = await client.get("/api/v1/stats/overview?scope=all", headers=auth)

    assert response.status_code == 403


async def test_an_admin_sees_everyone(client: AsyncClient, db_session: AsyncSession) -> None:
    auth = await sign_in(client, db_session, email="bigadmin@example.test", role=UserRole.ADMIN)
    other = await make_user(db_session, email="somebody@example.test")
    await finished(db_session, other, status=TaskStatus.COMPLETED, seconds=10)
    await db_session.commit()

    body = (await client.get("/api/v1/stats/overview?scope=all", headers=auth)).json()

    assert body["scope"] == "all"
    assert body["tasksTotal"] >= 1
