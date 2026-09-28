"""Input validation, rate limits and access control (spec section 10).

The edge cases found by reviewing every endpoint, and the two rate limits added
with them. Anything that can be checked without a database is in
`tests/unit/test_security_headers.py`; these need real rows.
"""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_screenshot_storage, get_task_runner
from app.core.config import Settings
from app.domain.enums import ActionType, ApprovalStatus, TaskStatus, UserRole
from app.models.approval import Approval
from app.models.task import TaskStep
from app.repositories.unit_of_work import UnitOfWork
from app.services.storage import LocalFileStorage
from tests.integration.conftest_helpers import make_task, make_user

pytestmark = pytest.mark.integration

INSTRUCTION = "Find the cheapest flight from Delhi to Bengaluru on 20 Oct"


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
def storage(settings: Settings, tmp_path: Path) -> LocalFileStorage:
    return LocalFileStorage(settings.model_copy(update={"STORAGE_DIR": str(tmp_path)}))


@pytest.fixture
def security_app(api_app: FastAPI, storage: LocalFileStorage) -> FastAPI:
    api_app.dependency_overrides[get_task_runner] = lambda: FakeRunner()
    api_app.dependency_overrides[get_screenshot_storage] = lambda: storage
    api_app.state.screenshot_storage = storage
    return api_app


@pytest.fixture
async def client(security_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=security_app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def sign_in(
    client: AsyncClient,
    db_session: AsyncSession,
    *,
    email: str,
    role: UserRole = UserRole.TASK_SUBMITTER,
) -> dict[str, str]:
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


async def submit(client: AsyncClient, auth: dict[str, str], **body: Any) -> Any:
    return await client.post(
        "/api/v1/tasks", json={"instruction": INSTRUCTION, **body}, headers=auth
    )


# ------------------------------------------------------------ the instruction


async def test_a_control_character_in_an_instruction_is_stripped_not_stored(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A NUL byte in a text column is a Postgres error, so this would be a 500.

    Found by reviewing what reaches the database rather than by a failure: the
    length and the whitespace were checked, and the bytes in between were not.
    """
    auth = await sign_in(client, db_session, email="control@example.test")

    response = await submit(client, auth, instruction="Find the\x00 cheapest\x07 flight")

    assert response.status_code == 201, response.text
    assert response.json()["instruction"] == "Find the cheapest flight"


async def test_an_instruction_of_only_control_characters_is_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Stripping happens before the length check, so this is a 400, not a row."""
    auth = await sign_in(client, db_session, email="allcontrol@example.test")

    response = await submit(client, auth, instruction="\x00\x01\x02\x03\x04\x05")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


@pytest.mark.parametrize("length", [4, 2001])
async def test_an_instruction_outside_the_length_bounds_is_rejected(
    client: AsyncClient, db_session: AsyncSession, length: int
) -> None:
    auth = await sign_in(client, db_session, email=f"len{length}@example.test")

    response = await submit(client, auth, instruction="a" * length)

    assert response.status_code == 400


# -------------------------------------------------------------- the start URL


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ftp://localhost/x",
        "//localhost/x",
        "localhost:8200",
    ],
)
async def test_a_start_url_with_the_wrong_scheme_is_rejected(
    client: AsyncClient, db_session: AsyncSession, url: str
) -> None:
    auth = await sign_in(client, db_session, email="scheme@example.test")

    response = await submit(client, auth, startUrl=url)

    assert response.status_code == 400, response.text


async def test_a_start_url_with_credentials_is_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """They would be stored on the row and repeated in every event about it.

    The allow-list check parses the hostname correctly either way, so this is
    about not keeping somebody's password in the activity log.
    """
    auth = await sign_in(client, db_session, email="userinfo@example.test")

    response = await submit(client, auth, startUrl="http://user:secret@localhost:8200/")

    assert response.status_code == 400
    assert "credentials" in response.json()["error"]["message"].lower()


async def test_a_start_url_outside_the_allow_list_is_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Spec section 4.13. The scheme is a schema rule; the host is configuration."""
    auth = await sign_in(client, db_session, email="offlist@example.test")

    response = await submit(client, auth, startUrl="https://evil.test/")

    assert response.status_code == 400


async def test_a_host_that_merely_contains_an_allowed_one_is_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """``evil-localhost.test`` must not pass because the string contains localhost."""
    auth = await sign_in(client, db_session, email="substring@example.test")

    response = await submit(client, auth, startUrl="https://evil-localhost.test/")

    assert response.status_code == 400


# --------------------------------------------------------------- the listings


async def test_pagination_is_capped(client: AsyncClient, db_session: AsyncSession) -> None:
    """Decision A17: page size 20 by default, 100 at most."""
    auth = await sign_in(client, db_session, email="paging@example.test")

    too_big = await client.get("/api/v1/tasks?pageSize=1000", headers=auth)
    zero = await client.get("/api/v1/tasks?pageSize=0", headers=auth)
    negative_page = await client.get("/api/v1/tasks?page=0", headers=auth)

    assert too_big.status_code == 400
    assert zero.status_code == 400
    assert negative_page.status_code == 400


async def test_an_unknown_status_filter_is_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    auth = await sign_in(client, db_session, email="statusfilter@example.test")

    response = await client.get("/api/v1/tasks?status=NOT_A_STATUS", headers=auth)

    assert response.status_code == 400


async def test_an_inverted_date_range_is_rejected_rather_than_empty(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """An empty page reads as "you have no tasks", which sends the user looking
    for the tasks rather than for the typo in their dates."""
    auth = await sign_in(client, db_session, email="dates@example.test")

    response = await client.get(
        "/api/v1/tasks?from=2026-02-01T00:00:00Z&to=2026-01-01T00:00:00Z", headers=auth
    )

    assert response.status_code == 400


async def test_a_search_term_with_wildcards_is_matched_literally(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """``%`` in a LIKE pattern would otherwise match every task."""
    auth = await sign_in(client, db_session, email="wildcard@example.test")
    await submit(client, auth)

    response = await client.get("/api/v1/tasks?q=%25", headers=auth)

    assert response.status_code == 200
    assert response.json()["total"] == 0


# ------------------------------------------------------------- the approvals


async def test_a_body_that_disagrees_with_the_endpoint_is_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """``{"approved": false}`` posted to ``/approve``.

    The action on the other side cannot be undone, and either reading of the
    request might be the one nobody wanted, so neither is guessed at.
    """
    auth = await sign_in(client, db_session, email="disagree@example.test", role=UserRole.ADMIN)
    task = (await submit(client, auth)).json()

    approve = await client.post(
        f"/api/v1/tasks/{task['id']}/approve", json={"approved": False}, headers=auth
    )
    reject = await client.post(
        f"/api/v1/tasks/{task['id']}/reject", json={"approved": True}, headers=auth
    )

    assert approve.status_code == 400
    assert reject.status_code == 400
    assert "approved" in approve.json()["error"]["message"]


async def test_an_over_long_approval_reason_is_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    auth = await sign_in(client, db_session, email="longreason@example.test", role=UserRole.ADMIN)
    task = (await submit(client, auth)).json()

    response = await client.post(
        f"/api/v1/tasks/{task['id']}/reject",
        json={"approved": False, "reason": "x" * 501},
        headers=auth,
    )

    assert response.status_code == 400


async def test_approvals_are_rate_limited(client: AsyncClient, db_session: AsyncSession) -> None:
    """Cheap to call, and not cheap to serve: each one writes an audit row."""
    auth = await sign_in(client, db_session, email="ratelimit@example.test", role=UserRole.ADMIN)
    task = (await submit(client, auth)).json()

    statuses = [
        (
            await client.post(
                f"/api/v1/tasks/{task['id']}/reject", json={"approved": False}, headers=auth
            )
        ).status_code
        for _ in range(25)
    ]

    assert 429 in statuses


async def test_stream_tickets_are_rate_limited(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A ticket is a credential, so minting them is not unlimited."""
    auth = await sign_in(client, db_session, email="tickets@example.test")
    task = (await submit(client, auth)).json()

    statuses = [
        (await client.post(f"/api/v1/tasks/{task['id']}/stream-ticket", headers=auth)).status_code
        for _ in range(35)
    ]

    assert 429 in statuses


# ------------------------------------------------------------- the grounding


@pytest.mark.parametrize("description", ["", "x", "y" * 401])
async def test_a_grounding_description_outside_the_bounds_is_rejected(
    client: AsyncClient, db_session: AsyncSession, description: str
) -> None:
    auth = await sign_in(client, db_session, email=f"ground{len(description)}@example.test")
    task = (await submit(client, auth)).json()

    response = await client.post(
        f"/api/v1/tasks/{task['id']}/ground",
        json={"description": description},
        headers=auth,
    )

    assert response.status_code == 400


# ------------------------------------------------------------ the screenshots


async def test_a_screenshot_is_served_only_to_its_owner(
    client: AsyncClient, db_session: AsyncSession, storage: LocalFileStorage
) -> None:
    """Spec section 10, and a 404 rather than a 403.

    A stranger must not be able to learn that the task exists by the shape of
    the refusal.
    """
    owner_auth = await sign_in(client, db_session, email="shotowner@example.test")
    uow = UnitOfWork(db_session)
    owner = await uow.users.get_by_email("shotowner@example.test")
    assert owner is not None

    task = await make_task(db_session, owner, INSTRUCTION)
    step = TaskStep(task_id=task.id, step_number=1, action=ActionType.CLICK)
    db_session.add(step)
    await db_session.flush()
    step.screenshot_ref = await storage.save(task.id, 1, b"\xff\xd8\xff-jpeg")
    await db_session.commit()

    mine = await client.get(
        f"/api/v1/tasks/{task.id}/steps/{step.id}/screenshot", headers=owner_auth
    )
    stranger_auth = await sign_in(client, db_session, email="shotstranger@example.test")
    theirs = await client.get(
        f"/api/v1/tasks/{task.id}/steps/{step.id}/screenshot", headers=stranger_auth
    )
    anonymous = await client.get(f"/api/v1/tasks/{task.id}/steps/{step.id}/screenshot")

    assert mine.status_code == 200
    assert mine.headers["content-type"] == "image/jpeg"
    assert theirs.status_code == 404
    assert anonymous.status_code == 401


async def test_an_admin_can_read_any_screenshot(
    client: AsyncClient, db_session: AsyncSession, storage: LocalFileStorage
) -> None:
    uow = UnitOfWork(db_session)
    owner = await make_user(db_session, email="someone-else@example.test")
    task = await make_task(db_session, owner, INSTRUCTION)
    step = TaskStep(task_id=task.id, step_number=1, action=ActionType.CLICK)
    db_session.add(step)
    await db_session.flush()
    step.screenshot_ref = await storage.save(task.id, 1, b"\xff\xd8\xff-jpeg")
    await db_session.commit()

    admin_auth = await sign_in(
        client, db_session, email="shotadmin@example.test", role=UserRole.ADMIN
    )
    response = await client.get(
        f"/api/v1/tasks/{task.id}/steps/{step.id}/screenshot", headers=admin_auth
    )

    assert response.status_code == 200
    assert uow is not None


async def test_a_step_from_another_task_cannot_be_read_through_your_own(
    client: AsyncClient, db_session: AsyncSession, storage: LocalFileStorage
) -> None:
    """The step is pinned to the task in the path, not just looked up by id."""
    auth = await sign_in(client, db_session, email="pinned@example.test")
    uow = UnitOfWork(db_session)
    mine = await uow.users.get_by_email("pinned@example.test")
    assert mine is not None
    other = await make_user(db_session, email="pinned-other@example.test")

    my_task = await make_task(db_session, mine, INSTRUCTION)
    their_task = await make_task(db_session, other, INSTRUCTION)
    their_step = TaskStep(task_id=their_task.id, step_number=1, action=ActionType.CLICK)
    db_session.add(their_step)
    await db_session.flush()
    their_step.screenshot_ref = await storage.save(their_task.id, 1, b"\xff\xd8\xff-jpeg")
    await db_session.commit()

    response = await client.get(
        f"/api/v1/tasks/{my_task.id}/steps/{their_step.id}/screenshot", headers=auth
    )

    assert response.status_code == 404


async def test_nothing_serves_the_screenshot_directory(security_app: FastAPI) -> None:
    """No directory listing, because nothing mounts the storage root at all.

    The only way to a screenshot is the endpoint above, which checks ownership.
    Asserted on the route table rather than by probing a path, so a future
    ``StaticFiles`` mount fails this test rather than quietly exposing the tree.
    """
    mounts = [route for route in security_app.routes if type(route).__name__ == "Mount"]

    assert mounts == []


# ------------------------------------------------------------------- the RBAC


async def test_an_ordinary_user_cannot_answer_an_approval_they_do_not_own(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Spec section 10 asks for RBAC on approvals."""
    other = await make_user(db_session, email="approval-owner@example.test")
    task = await make_task(db_session, other, INSTRUCTION)
    task.status = TaskStatus.AWAITING_APPROVAL
    db_session.add(
        Approval(
            task_id=task.id,
            proposed_action={"action": "click"},
            risk_flag="HIGH",
            approval_status=ApprovalStatus.PENDING,
        )
    )
    await db_session.commit()

    auth = await sign_in(client, db_session, email="nosy@example.test")
    response = await client.post(
        f"/api/v1/tasks/{task.id}/approve", json={"approved": True}, headers=auth
    )

    assert response.status_code == 404
