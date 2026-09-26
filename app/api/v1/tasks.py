"""Task and step endpoints (spec section 6).

Thin on purpose. Every rule about what a task is, who may see it and when it
may be cancelled lives in :mod:`app.services.task_service`; these handlers
translate HTTP into a service call and a response model.

Each route carries a summary, a description and the error responses it can
return, because the frontend generates its TypeScript from ``/openapi.json``
and an endpoint that is undocumented there is an endpoint it cannot call.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import CurrentUser, TaskRunnerDep, TaskServiceDep, rate_limit
from app.db.session import AfterCommitDep
from app.domain.enums import TaskStatus
from app.models.task import Task
from app.repositories.base import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page
from app.schemas.task import (
    CreateTaskRequest,
    LlmDecisionResponse,
    RecoveryAttemptResponse,
    StepDetailResponse,
    StepResponse,
    TaskListResponse,
    TaskResponse,
    VerificationResponse,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])

_NOT_YOURS = {"description": "No such task, or it belongs to someone else"}


def _page_response(page: Page[Task]) -> TaskListResponse:
    return TaskListResponse(
        items=[TaskResponse.model_validate(task) for task in page.items],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
        total_pages=page.total_pages,
        has_next=page.has_next,
        has_previous=page.has_previous,
    )


@router.post(
    "",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a task",
    description=(
        "Queues a task and returns it in SUBMITTED. The agent starts when a "
        "slot frees, so a task can sit in SUBMITTED for a while on a busy "
        "server. Follow it with GET /tasks/{id} or the live stream."
    ),
    dependencies=[
        Depends(rate_limit("task_create", lambda s: s.RATE_LIMIT_TASK_CREATE_PER_MINUTE))
    ],
    responses={
        400: {
            "description": (
                "The instruction is missing, too short or too long, or the "
                "start URL is not http(s) or not in the allowed domain list"
            )
        },
        409: {"description": "You already have a live task with this instruction"},
        429: {"description": "Too many submissions"},
    },
)
async def create_task(
    payload: CreateTaskRequest,
    user: CurrentUser,
    tasks: TaskServiceDep,
    runner: TaskRunnerDep,
    after_commit: AfterCommitDep,
) -> TaskResponse:
    task = await tasks.create(
        user,
        instruction=payload.instruction,
        start_url=payload.start_url,
    )

    # After the commit, not in a background task. The agent opens its own
    # session and would otherwise look for a row this request has not written
    # yet, and find nothing.
    after_commit.append(lambda: runner.submit(task.id))

    return TaskResponse.model_validate(task)


@router.get(
    "",
    response_model=TaskListResponse,
    summary="List your tasks",
    description=(
        "Newest first. Admins see every task; everyone else sees their own. "
        "`status` may be repeated to match any of several states."
    ),
)
async def list_tasks(
    user: CurrentUser,
    tasks: TaskServiceDep,
    status_filter: Annotated[
        list[TaskStatus] | None,
        Query(alias="status", description="Repeatable. Matches any of the given states."),
    ] = None,
    created_from: Annotated[
        datetime | None,
        Query(alias="from", description="Only tasks created at or after this time."),
    ] = None,
    created_to: Annotated[
        datetime | None,
        Query(alias="to", description="Only tasks created at or before this time."),
    ] = None,
    q: Annotated[
        str | None,
        Query(max_length=200, description="Case-insensitive search of the instruction."),
    ] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> TaskListResponse:
    results = await tasks.list(
        user,
        status=status_filter,
        created_from=created_from,
        created_to=created_to,
        query=q,
        page=page,
        page_size=page_size,
    )
    return _page_response(results)


@router.get(
    "/{task_id}",
    response_model=TaskResponse,
    summary="Get one task",
    responses={404: _NOT_YOURS},
)
async def get_task(task_id: uuid.UUID, user: CurrentUser, tasks: TaskServiceDep) -> TaskResponse:
    return TaskResponse.model_validate(await tasks.get(task_id, user))


@router.post(
    "/{task_id}/cancel",
    response_model=TaskResponse,
    summary="Cancel a task",
    description=(
        "Stops a task that is still running. The agent finishes the step it is "
        "on, records it, and stops there, so the activity log always matches "
        "what the browser actually did."
    ),
    responses={
        404: _NOT_YOURS,
        409: {"description": "The task has already finished"},
    },
)
async def cancel_task(
    task_id: uuid.UUID,
    user: CurrentUser,
    tasks: TaskServiceDep,
    runner: TaskRunnerDep,
    after_commit: AfterCommitDep,
) -> TaskResponse:
    task = await tasks.cancel(task_id, user)

    # The row is already CANCELLED, which is what actually stops the loop; the
    # signal only makes it notice sooner. Still after the commit, so a loop
    # that reads the row on waking sees the same answer the flag gave it.
    def signal_the_runner() -> None:
        runner.cancel_soon(task_id)

    after_commit.append(signal_the_runner)

    return TaskResponse.model_validate(task)


@router.get(
    "/{task_id}/steps",
    response_model=list[StepResponse],
    summary="List a task's steps",
    description="In order. This is the activity log, without the per-step detail.",
    responses={404: _NOT_YOURS},
)
async def list_steps(
    task_id: uuid.UUID,
    user: CurrentUser,
    tasks: TaskServiceDep,
) -> list[StepResponse]:
    steps = await tasks.list_steps(task_id, user)
    return [StepResponse.model_validate(step) for step in steps]


@router.get(
    "/{task_id}/steps/{step_id}",
    response_model=StepDetailResponse,
    summary="Get one step in full",
    description=(
        "The step, the decision the model made, how it was verified, and every "
        "recovery attempt against it."
    ),
    responses={404: {"description": "No such step on this task"}},
)
async def get_step(
    task_id: uuid.UUID,
    step_id: uuid.UUID,
    user: CurrentUser,
    tasks: TaskServiceDep,
) -> StepDetailResponse:
    detail = await tasks.get_step(task_id, step_id, user)

    # Built from the base model rather than validated off the ORM row: the
    # relationships are lazy="raise", so reading them here would either raise
    # or, worse, quietly emit three queries per step.
    base = StepResponse.model_validate(detail.step)
    return StepDetailResponse(
        **base.model_dump(),
        llm_decision=(
            LlmDecisionResponse.model_validate(detail.decision) if detail.decision else None
        ),
        verification=(
            VerificationResponse.model_validate(detail.verification)
            if detail.verification
            else None
        ),
        recovery_attempts=[
            RecoveryAttemptResponse.model_validate(attempt) for attempt in detail.recovery_attempts
        ],
    )


@router.get(
    "/{task_id}/steps/{step_id}/screenshot",
    summary="Get a step's screenshot",
    description=(
        "The JPEG captured just after the step ran. Served only to the task's "
        "owner or an admin, and marked private so no shared cache keeps a copy."
    ),
    response_class=Response,
    responses={
        200: {"content": {"image/jpeg": {}}, "description": "The frame"},
        404: {"description": "No such step, or it has no screenshot"},
    },
)
async def get_step_screenshot(
    task_id: uuid.UUID,
    step_id: uuid.UUID,
    user: CurrentUser,
    tasks: TaskServiceDep,
) -> Response:
    image = await tasks.screenshot(task_id, step_id, user)
    return Response(
        content=image,
        media_type="image/jpeg",
        headers={
            # Spec section 10. A screenshot can show anything the page showed,
            # including somebody's account details, so no shared cache and no
            # proxy gets to keep a copy.
            "Cache-Control": "private, max-age=0, no-store",
        },
    )
