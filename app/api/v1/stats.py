"""The history screen's summary (spec section 9).

One endpoint, one round trip. The frontend's history page needs a completion
rate, a median duration, the recovery and cache rates, how many approvals were
asked for and a week of trend, and fetching those separately would be six
requests to render one header.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, StatsServiceDep
from app.schemas.task import StatsOverviewResponse, TrendPointResponse

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get(
    "/overview",
    response_model=StatsOverviewResponse,
    summary="Your task statistics",
    description=(
        "Your own finished tasks by default. An administrator may pass "
        "`scope=all` for the whole deployment; anyone else asking for it gets "
        "403 rather than their own numbers under a global heading.\n\n"
        "Rates are null when there is nothing to divide by, because zero would "
        "claim that nothing succeeded."
    ),
    responses={403: {"description": "Only an administrator can read the global statistics"}},
)
async def overview(
    user: CurrentUser,
    stats: StatsServiceDep,
    scope: Annotated[Literal["mine", "all"], Query()] = "mine",
) -> StatsOverviewResponse:
    summary = await stats.overview(user, scope=scope)
    return StatsOverviewResponse(
        scope=summary.scope,
        tasks_total=summary.tasks_total,
        tasks_completed=summary.tasks_completed,
        completion_rate=summary.completion_rate,
        median_duration_seconds=summary.median_duration_seconds,
        recovery_success_rate=summary.recovery_success_rate,
        recovery_episodes=summary.recovery_episodes,
        cache_hit_rate=summary.cache_hit_rate,
        approvals_requested=summary.approvals_requested,
        credits_used=summary.credits_used,
        trend=[
            TrendPointResponse(day=point.day, tasks=point.tasks, completed=point.completed)
            for point in summary.trend
        ],
    )
