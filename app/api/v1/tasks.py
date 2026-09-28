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

from app.agent.llm.factory import build_llm
from app.agent.vision.grounding import VisualGroundingService
from app.api.deps import (
    ApprovalServiceDep,
    CurrentUser,
    SettingsDep,
    TaskRunnerDep,
    TaskServiceDep,
    rate_limit,
)
from app.core.errors import Conflict
from app.db.session import AfterCommitDep
from app.domain.enums import TaskStatus
from app.models.task import Task
from app.repositories.base import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page
from app.schemas.task import (
    ApprovalDecisionRequest,
    ApprovalResponse,
    CreateTaskRequest,
    GroundRequest,
    GroundResponse,
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


async def _decide(
    task_id: uuid.UUID,
    approved: bool,
    reason: str | None,
    user: CurrentUser,
    approvals: ApprovalServiceDep,
    after_commit: AfterCommitDep,
) -> ApprovalResponse:
    """Answer the approval a task is parked on.

    The decision is committed before it is announced. The run that is waiting
    wakes and re-reads the row, so it must find the answer that woke it there.
    """
    approval = await approvals.decide(task_id, user, approved=approved, reason=reason)
    response = ApprovalResponse.model_validate(
        {
            "id": approval.id,
            "task_id": approval.task_id,
            "status": approval.approval_status,
            "risk_flag": approval.risk_flag,
            "proposed_action": approval.proposed_action,
            "reason": approval.reason,
            "approved_by": approval.approved_by,
            "decided_at": approval.decided_at,
            "created_at": approval.created_at,
        }
    )

    def announce() -> None:
        import asyncio

        asyncio.ensure_future(  # noqa: RUF006
            approvals.announce(approval, approved=approved, by=user.id)
        )

    after_commit.append(announce)
    return response


@router.post(
    "/{task_id}/approve",
    response_model=ApprovalResponse,
    summary="Approve a high-risk action",
    description=(
        "Lets a task that is parked on a human carry on. Allowed for APPROVER "
        "and ADMIN, and for the task's own submitter when the deployment "
        "permits it."
    ),
    responses={
        403: {"description": "Your role does not allow approving"},
        404: {"description": "No such task, or it is not waiting for an approval"},
        409: {"description": "That approval has already been answered"},
    },
)
async def approve_task(
    task_id: uuid.UUID,
    payload: ApprovalDecisionRequest,
    user: CurrentUser,
    approvals: ApprovalServiceDep,
    after_commit: AfterCommitDep,
) -> ApprovalResponse:
    return await _decide(task_id, True, payload.reason, user, approvals, after_commit)


@router.post(
    "/{task_id}/reject",
    response_model=ApprovalResponse,
    summary="Reject a high-risk action",
    description=(
        "Stops the task. A rejection is a person having looked and said no, "
        "which ends the run as REJECTED; an approval nobody answers is a "
        "different thing and times out instead."
    ),
    responses={
        403: {"description": "Your role does not allow approving"},
        404: {"description": "No such task, or it is not waiting for an approval"},
        409: {"description": "That approval has already been answered"},
    },
)
async def reject_task(
    task_id: uuid.UUID,
    payload: ApprovalDecisionRequest,
    user: CurrentUser,
    approvals: ApprovalServiceDep,
    after_commit: AfterCommitDep,
) -> ApprovalResponse:
    return await _decide(task_id, False, payload.reason, user, approvals, after_commit)


@router.post(
    "/{task_id}/ground",
    response_model=GroundResponse,
    summary="Ask vision where something is on the current page",
    description=(
        "Runs visual grounding against the page the agent is looking at right "
        "now and returns what it found. It does not click, type or otherwise "
        "act. Only works while the task is running on this worker."
    ),
    responses={
        404: _NOT_YOURS,
        409: {"description": "The task is not running, so it has no current page"},
    },
)
async def ground(
    task_id: uuid.UUID,
    payload: GroundRequest,
    user: CurrentUser,
    tasks: TaskServiceDep,
    runner: TaskRunnerDep,
    settings: SettingsDep,
) -> GroundResponse:
    task = await tasks.get(task_id, user)
    if TaskStatus(task.status).is_terminal:
        raise Conflict(f"This task has already finished ({task.status.value}).")

    browser = runner.browser_for(task_id)
    if browser is None:
        raise Conflict("This task is not running on this worker, so it has no current page.")

    service = VisualGroundingService(
        build_llm(settings),
        version=settings.VISION_PROMPT_VERSION,
        max_width=settings.VISION_MAX_WIDTH,
        max_image_bytes=settings.VISION_MAX_IMAGE_BYTES,
    )
    state = await browser.perceive()
    shot = await browser.screenshot()
    context = service.build_evidence_context(state, shot.jpeg_bytes, payload.description)
    result = await service.resolve_target(context)

    return GroundResponse(
        resolved=result.resolved,
        element_index=result.element_index,
        point={"x": result.point[0], "y": result.point[1]} if result.point else None,
        bbox=result.bbox,
        confidence=result.confidence,
        justification=result.justification,
        used_vision=result.used_vision,
        latency_ms=result.latency_ms,
        error=result.error,
    )


@router.get(
    "/{task_id}/recovery-attempts",
    response_model=list[RecoveryAttemptResponse],
    summary="List a task's recovery attempts",
    description=(
        "Every rung of the recovery ladder this task climbed, across all "
        "steps, in order. Empty for a run where nothing needed rescuing."
    ),
    responses={404: _NOT_YOURS},
)
async def list_recovery_attempts(
    task_id: uuid.UUID,
    user: CurrentUser,
    tasks: TaskServiceDep,
) -> list[RecoveryAttemptResponse]:
    attempts = await tasks.list_recovery_attempts(task_id, user)
    return [RecoveryAttemptResponse.model_validate(attempt) for attempt in attempts]


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
