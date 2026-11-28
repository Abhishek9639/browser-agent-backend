"""B19: stream tickets over HTTP, and replay built from real rows."""

import base64
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.enums import ActionType, ApprovalStatus, RiskLevel, StepStatus, TaskStatus
from app.models.approval import Approval
from app.models.task import LLMDecision, TaskStep
from app.repositories.unit_of_work import UnitOfWork
from app.services.events import EventType
from app.services.storage import LocalFileStorage
from app.services.stream import StreamService
from tests.integration.conftest_helpers import make_task, make_user

ACCOUNT = {"name": "Watcher", "email": "watcher@example.test", "password": "a-long-passphrase"}
JPEG = b"\xff\xd8\xff-pretend-jpeg"


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


@pytest.fixture
def storage(settings: Settings, tmp_path: Path) -> LocalFileStorage:
    return LocalFileStorage(settings.model_copy(update={"STORAGE_DIR": str(tmp_path)}))


@pytest.fixture
def service(db_session: AsyncSession, storage: LocalFileStorage) -> StreamService:
    return StreamService(session_unit_of_work(db_session), storage)


async def add_step(
    session: AsyncSession,
    task_id: uuid.UUID,
    number: int,
    *,
    storage: LocalFileStorage | None = None,
    status: StepStatus = StepStatus.VERIFIED,
) -> TaskStep:
    reference = await storage.save(task_id, number, JPEG) if storage else None
    step = TaskStep(
        task_id=task_id,
        step_number=number,
        action=ActionType.CLICK,
        target=f"Select {number}",
        value_redacted=None,
        tab_id="t1",
        bbox={"x": 1, "y": 2, "width": 3, "height": 4},
        screenshot_ref=reference,
        status=status,
    )
    session.add(step)
    await session.flush()
    return step


# ----------------------------------------------------------- ticket route


@pytest.fixture
async def client(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def sign_in(client: AsyncClient, **overrides: Any) -> dict[str, str]:
    body = {**ACCOUNT, **overrides}
    await client.post("/api/v1/auth/register", json=body)
    response = await client.post(
        "/api/v1/auth/login", json={"email": body["email"], "password": body["password"]}
    )
    return {"Authorization": f"Bearer {response.json()['accessToken']}"}


async def test_a_ticket_is_issued_for_your_own_task(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    auth = await sign_in(client)
    me = (await client.get("/api/v1/auth/me", headers=auth)).json()
    task = await make_task(
        db_session, await _user_row(db_session, me["id"]), instruction="Watch me"
    )
    await db_session.commit()

    response = await client.post(f"/api/v1/tasks/{task.id}/stream-ticket", headers=auth)

    assert response.status_code == 200
    body = response.json()
    assert body["ticket"]
    assert body["expiresIn"] == 60
    # The access token stays on this request. It must never reach the socket
    # URL, where it would land in a server log and the browser history.
    assert body["ticket"] not in auth["Authorization"]


async def test_each_call_gives_a_fresh_ticket(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    auth = await sign_in(client)
    me = (await client.get("/api/v1/auth/me", headers=auth)).json()
    task = await make_task(db_session, await _user_row(db_session, me["id"]))
    await db_session.commit()

    first = await client.post(f"/api/v1/tasks/{task.id}/stream-ticket", headers=auth)
    second = await client.post(f"/api/v1/tasks/{task.id}/stream-ticket", headers=auth)

    assert first.json()["ticket"] != second.json()["ticket"]


async def test_no_ticket_for_someone_elses_task(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    auth = await sign_in(client)
    owner = await make_user(db_session, "owner-of-it@example.test")
    task = await make_task(db_session, owner)
    await db_session.commit()

    response = await client.post(f"/api/v1/tasks/{task.id}/stream-ticket", headers=auth)

    assert response.status_code == 404


async def test_no_ticket_without_a_token(client: AsyncClient) -> None:
    response = await client.post(f"/api/v1/tasks/{uuid.uuid4()}/stream-ticket")

    assert response.status_code == 401


async def _user_row(session: AsyncSession, user_id: str) -> Any:
    from app.models.user import User

    return await session.get(User, uuid.UUID(user_id))


# ----------------------------------------------------------------- access


async def test_access_allows_the_owner(service: StreamService, db_session: AsyncSession) -> None:
    owner = await make_user(db_session, "a@example.test")
    task = await make_task(db_session, owner)
    await db_session.commit()

    assert await service.access(task.id, owner.id) == "ok"


async def test_access_refuses_someone_else(
    service: StreamService, db_session: AsyncSession
) -> None:
    owner = await make_user(db_session, "b@example.test")
    stranger = await make_user(db_session, "c@example.test")
    task = await make_task(db_session, owner)
    await db_session.commit()

    assert await service.access(task.id, stranger.id) == "forbidden"


async def test_access_reports_a_missing_task(service: StreamService) -> None:
    assert await service.access(uuid.uuid4(), uuid.uuid4()) == "missing"


# ----------------------------------------------------------------- replay


async def test_replay_starts_with_the_current_status(
    service: StreamService, db_session: AsyncSession
) -> None:
    owner = await make_user(db_session, "d@example.test")
    task = await make_task(db_session, owner, status=TaskStatus.ACTING)
    await db_session.commit()

    replay = await service.build(task.id)

    assert replay is not None
    assert replay.events[0].type is EventType.STATUS
    assert replay.events[0].status is TaskStatus.ACTING
    assert replay.is_terminal is False


async def test_replay_carries_every_step_with_its_screenshot(
    service: StreamService, db_session: AsyncSession, storage: LocalFileStorage
) -> None:
    owner = await make_user(db_session, "e@example.test")
    task = await make_task(db_session, owner, status=TaskStatus.ACTING)
    for number in (1, 2, 3):
        await add_step(db_session, task.id, number, storage=storage)
    await db_session.commit()

    replay = await service.build(task.id)

    assert replay is not None
    steps = [event for event in replay.events if event.type is EventType.STEP]
    assert [event.step_number for event in steps] == [1, 2, 3]
    assert all(event.screenshot == base64.b64encode(JPEG).decode() for event in steps)
    assert steps[0].bbox is not None and steps[0].bbox.width == 3
    assert steps[0].verified is True


async def test_replay_carries_what_the_live_step_showed(
    service: StreamService, db_session: AsyncSession, storage: LocalFileStorage
) -> None:
    """A run opened from history reads like one watched live: why the agent did
    each step, the page it was on, and the viewport its outline box is drawn in."""
    owner = await make_user(db_session, "detail@example.test")
    task = await make_task(db_session, owner, status=TaskStatus.COMPLETED)
    step = await add_step(db_session, task.id, 1, storage=storage)
    step.url = "https://in.bookmyshow.com/explore/movies-bengaluru"
    step.viewport_width, step.viewport_height = 1440, 812
    db_session.add(
        LLMDecision(
            step_id=step.id,
            prompt_version="v2",
            model_name="gemini-3-flash-preview",
            provider="gemini",
            reasoning_summary="Clicking Book tickets to see the shows.",
            confidence=0.92,
            risk_level=RiskLevel.LOW,
            used_vision=True,
        )
    )
    await db_session.commit()

    replay = await service.build(task.id)

    assert replay is not None
    (event,) = [event for event in replay.events if event.type is EventType.STEP]
    assert event.reasoning == "Clicking Book tickets to see the shows."
    assert event.confidence == pytest.approx(0.92)
    assert event.used_vision is True
    assert event.url == "https://in.bookmyshow.com/explore/movies-bengaluru"
    assert event.viewport is not None
    assert (event.viewport.width, event.viewport.height) == (1440, 812)


async def test_a_step_saved_before_viewports_were_kept_still_replays(
    service: StreamService, db_session: AsyncSession, storage: LocalFileStorage
) -> None:
    owner = await make_user(db_session, "old-step@example.test")
    task = await make_task(db_session, owner, status=TaskStatus.COMPLETED)
    await add_step(db_session, task.id, 1, storage=storage)
    await db_session.commit()

    replay = await service.build(task.id)

    assert replay is not None
    (event,) = [event for event in replay.events if event.type is EventType.STEP]
    # No row to read them from: no viewport (so no outline box), no reasoning.
    assert event.viewport is None
    assert event.url is None
    assert event.reasoning is None
    assert event.screenshot is not None


async def test_replay_after_a_cursor_returns_only_what_was_missed(
    service: StreamService, db_session: AsyncSession
) -> None:
    owner = await make_user(db_session, "f@example.test")
    task = await make_task(db_session, owner, status=TaskStatus.ACTING)
    for number in range(1, 6):
        await add_step(db_session, task.id, number)
    await db_session.commit()

    replay = await service.build(task.id, after_step=3)

    assert replay is not None
    steps = [event for event in replay.events if event.type is EventType.STEP]
    assert [event.step_number for event in steps] == [4, 5]
    assert replay.last_step == 5


async def test_a_cursor_past_the_end_returns_no_steps(
    service: StreamService, db_session: AsyncSession
) -> None:
    owner = await make_user(db_session, "g@example.test")
    task = await make_task(db_session, owner, status=TaskStatus.ACTING)
    await add_step(db_session, task.id, 1)
    await db_session.commit()

    replay = await service.build(task.id, after_step=9)

    assert replay is not None
    assert [event for event in replay.events if event.type is EventType.STEP] == []


async def test_a_failed_step_is_replayed_as_unverified(
    service: StreamService, db_session: AsyncSession
) -> None:
    owner = await make_user(db_session, "h@example.test")
    task = await make_task(db_session, owner, status=TaskStatus.ACTING)
    await add_step(db_session, task.id, 1, status=StepStatus.FAILED)
    await db_session.commit()

    replay = await service.build(task.id)

    assert replay is not None
    step = next(event for event in replay.events if event.type is EventType.STEP)
    assert step.verified is False


async def test_a_missing_screenshot_does_not_take_the_replay_down(
    service: StreamService, db_session: AsyncSession
) -> None:
    owner = await make_user(db_session, "i@example.test")
    task = await make_task(db_session, owner, status=TaskStatus.ACTING)
    step = await add_step(db_session, task.id, 1)
    # A reference to a file that was never written, which is what a rolled-back
    # transaction or a swept storage directory leaves behind.
    step.screenshot_ref = f"{task.id}/0009.jpg"
    await db_session.commit()

    replay = await service.build(task.id)

    assert replay is not None
    event = next(item for item in replay.events if item.type is EventType.STEP)
    assert event.screenshot is None
    assert event.step_number == 1


async def test_a_pending_approval_is_replayed(
    service: StreamService, db_session: AsyncSession
) -> None:
    owner = await make_user(db_session, "j@example.test")
    task = await make_task(db_session, owner, status=TaskStatus.AWAITING_APPROVAL)
    approval = Approval(
        task_id=task.id,
        risk_flag=RiskLevel.HIGH,
        proposed_action={"action": "click", "target": "Pay now"},
        approval_status=ApprovalStatus.PENDING,
        reason="Irreversible",
    )
    db_session.add(approval)
    await db_session.commit()

    replay = await service.build(task.id)

    assert replay is not None
    asked = next(event for event in replay.events if event.type is EventType.APPROVAL_REQUIRED)
    # A client that reconnects while a human is being asked has to see the
    # prompt again, or nobody ever answers it.
    assert asked.approval_id == approval.id
    assert asked.risk_level is RiskLevel.HIGH
    assert asked.proposed_action["target"] == "Pay now"


async def test_an_answered_approval_is_not_replayed(
    service: StreamService, db_session: AsyncSession
) -> None:
    owner = await make_user(db_session, "k@example.test")
    task = await make_task(db_session, owner, status=TaskStatus.ACTING)
    db_session.add(
        Approval(
            task_id=task.id,
            risk_flag=RiskLevel.HIGH,
            proposed_action={"action": "click"},
            approval_status=ApprovalStatus.APPROVED,
        )
    )
    await db_session.commit()

    replay = await service.build(task.id)

    assert replay is not None
    assert not [e for e in replay.events if e.type is EventType.APPROVAL_REQUIRED]


async def test_a_finished_task_replays_and_ends_with_a_final_event(
    service: StreamService, db_session: AsyncSession
) -> None:
    owner = await make_user(db_session, "l@example.test")
    task = await make_task(db_session, owner, status=TaskStatus.COMPLETED)
    task.final_answer = {"text": "The cheapest option is Rs 4,210."}
    for number in (1, 2):
        await add_step(db_session, task.id, number)
    await db_session.commit()

    replay = await service.build(task.id)

    assert replay is not None
    assert replay.is_terminal is True
    assert [event.type.value for event in replay.events] == ["status", "step", "step", "final"]

    final = replay.events[-1]
    assert final.status is TaskStatus.COMPLETED
    assert final.final_answer == "The cheapest option is Rs 4,210."
    assert final.stats.steps == 2


async def test_a_failed_task_replays_its_reason(
    service: StreamService, db_session: AsyncSession
) -> None:
    owner = await make_user(db_session, "m@example.test")
    task = await make_task(db_session, owner, status=TaskStatus.EXECUTION_FAILED)
    task.failure_reason = "The task ran out of steps before it finished."
    await db_session.commit()

    replay = await service.build(task.id)

    assert replay is not None
    assert replay.events[-1].failure_reason == "The task ran out of steps before it finished."


async def test_replay_of_a_missing_task_is_none(service: StreamService) -> None:
    assert await service.build(uuid.uuid4()) is None


async def test_replayed_events_carry_sequence_zero(
    service: StreamService, db_session: AsyncSession
) -> None:
    owner = await make_user(db_session, "n@example.test")
    task = await make_task(db_session, owner, status=TaskStatus.ACTING)
    await add_step(db_session, task.id, 1)
    await db_session.commit()

    replay = await service.build(task.id)

    assert replay is not None
    # The original sequence number is not stored, and inventing one would
    # break the promise that a task's sequence only ever goes up. Steps resume
    # on stepNumber, which is stored, which is why that is the cursor.
    assert all(event.seq == 0 for event in replay.events)
