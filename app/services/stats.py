"""The numbers the history screen shows (spec section 9).

Arithmetic and access control. The SQL is in
:class:`app.repositories.stats.StatsRepository`; nothing here writes.

A rate over no tasks is ``None`` rather than zero. Zero is a claim that nothing
succeeded, which is a different and much worse thing to put in front of a user
than an empty state, and a frontend cannot tell the two apart once they have
the same value.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Final

from app.core.config import Settings
from app.core.errors import Forbidden
from app.core.logging import get_logger
from app.domain.enums import UserRole
from app.models.user import User
from app.repositories.unit_of_work import UnitOfWork

__all__ = ["TREND_DAYS", "DayPoint", "StatsOverview", "StatsService"]

logger = get_logger(__name__)

# A week, which is what the history page shows. Long enough to see a pattern,
# short enough that a bad afternoon is still visible in it.
TREND_DAYS: Final = 7

# Spec section 7 pricing, mirrored from the orchestrator so the endpoint can
# report a cost for work that finished before this process started.
CREDITS_PER_STEP: Final = 1
CREDITS_PER_VISION_STEP: Final = 2


@dataclass(frozen=True, slots=True)
class DayPoint:
    """One day of the trend."""

    day: date
    tasks: int
    completed: int


@dataclass(frozen=True, slots=True)
class StatsOverview:
    """What the history screen needs, in one round trip."""

    scope: str
    tasks_total: int = 0
    tasks_completed: int = 0
    completion_rate: float | None = None
    median_duration_seconds: float | None = None
    recovery_success_rate: float | None = None
    recovery_episodes: int = 0
    cache_hit_rate: float | None = None
    approvals_requested: int = 0
    credits_used: int | None = None
    trend: list[DayPoint] = field(default_factory=list)


class StatsService:
    """Reads the aggregates and turns them into rates."""

    def __init__(self, uow: UnitOfWork, settings: Settings) -> None:
        self._uow = uow
        self._settings = settings

    async def overview(self, user: User, *, scope: str = "mine") -> StatsOverview:
        """The whole summary for one caller.

        ``scope=all`` is the only thing an administrator gets that others do
        not, and it is refused rather than silently narrowed: a dashboard that
        quietly showed one user's numbers under a global heading would be worse
        than an error.
        """
        owner_id = self._owner_for(user, scope)

        totals = await self._uow.stats.task_totals(owner_id)
        median = await self._uow.stats.median_duration_seconds(owner_id)
        episodes, rescued = await self._uow.stats.recovery_episodes(owner_id)
        replayed = await self._uow.stats.replayed_tasks(owner_id)
        approvals = await self._uow.stats.approvals_requested(owner_id)
        since = datetime.now(UTC) - timedelta(days=TREND_DAYS - 1)
        daily = await self._uow.stats.daily(owner_id, since=since)

        credits: int | None = None
        if self._settings.BILLING_ENABLED:
            steps, vision = await self._uow.stats.step_and_vision_counts(owner_id)
            credits = (steps - vision) * CREDITS_PER_STEP + vision * CREDITS_PER_VISION_STEP

        overview = StatsOverview(
            scope="all" if owner_id is None else "mine",
            tasks_total=totals.total,
            tasks_completed=totals.completed,
            completion_rate=_rate(totals.completed, totals.total),
            median_duration_seconds=round(median, 2) if median is not None else None,
            recovery_success_rate=_rate(rescued, episodes),
            recovery_episodes=episodes,
            cache_hit_rate=_rate(replayed, totals.total),
            approvals_requested=approvals,
            credits_used=credits,
            trend=[DayPoint(day=d.day, tasks=d.total, completed=d.completed) for d in daily],
        )
        logger.info(
            "stats_overview",
            scope=overview.scope,
            tasks=overview.tasks_total,
            completion_rate=overview.completion_rate,
        )
        return overview

    def _owner_for(self, user: User, scope: str) -> uuid.UUID | None:
        if scope == "all":
            if user.role is not UserRole.ADMIN:
                raise Forbidden("Only an administrator can read the global statistics.")
            return None
        return user.id


def _rate(part: int, whole: int) -> float | None:
    """A share of a whole, or ``None`` when there is no whole.

    Rounded to three places, which is more than a percentage needs and stops a
    repeating fraction from reaching the client as fifteen digits of noise.
    """
    if whole <= 0:
        return None
    return round(part / whole, 3)
