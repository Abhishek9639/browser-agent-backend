"""Per-task budgets (spec section 4.2).

The loop condition is ``while step < MAX_STEPS and elapsed < MAX_TIME``. This
turns that into one object so the orchestrator asks a single question, and so
the reason a task stopped is recorded rather than inferred.

Both limits exist because they fail differently. A page that redirects in a
circle burns steps quickly and is caught by ``MAX_STEPS``. A page that hangs
burns no steps at all and would loop forever without ``MAX_TIME``.
"""

import time
from collections.abc import Callable
from enum import StrEnum
from typing import Self

from app.core.config import Settings

__all__ = ["BudgetExceeded", "BudgetReason", "TaskBudget"]


class BudgetReason(StrEnum):
    """Why a task ran out. Maps to ``tasks.failure_reason``."""

    MAX_STEPS = "MAX_STEPS"
    MAX_TIME = "MAX_TIME"


class BudgetExceeded(Exception):
    """A task hit one of its limits.

    Carries the reason so the orchestrator can set ``TIMED_OUT`` with an
    explanation a user can act on, rather than a bare failure.
    """

    def __init__(self, reason: BudgetReason, message: str | None = None) -> None:
        self.reason = reason
        self.message = message or f"Task budget exhausted: {reason.value}."
        super().__init__(self.message)


class TaskBudget:
    """Step and wall-clock allowance for one task run.

    The clock is injectable and monotonic. Monotonic because a wall-clock
    adjustment mid-task must not hand out extra seconds or cut a run short;
    injectable because testing a five minute timeout by waiting five minutes is
    not a test anyone will run.
    """

    def __init__(
        self,
        max_steps: int,
        max_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_steps <= 0:
            raise ValueError("max_steps must be positive")
        if max_seconds <= 0:
            raise ValueError("max_seconds must be positive")

        self.max_steps = max_steps
        self.max_seconds = max_seconds
        self._clock = clock
        self._started_at = clock()
        self._steps_used = 0

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> Self:
        return cls(
            max_steps=settings.AGENT_MAX_STEPS,
            max_seconds=settings.AGENT_MAX_TASK_SECONDS,
            clock=clock,
        )

    # ------------------------------------------------------------ state

    @property
    def steps_used(self) -> int:
        return self._steps_used

    @property
    def steps_remaining(self) -> int:
        return max(0, self.max_steps - self._steps_used)

    @property
    def elapsed_seconds(self) -> float:
        return self._clock() - self._started_at

    @property
    def seconds_remaining(self) -> float:
        return max(0.0, self.max_seconds - self.elapsed_seconds)

    @property
    def exhausted(self) -> bool:
        """Non-raising form, for a caller that wants to ask rather than try."""
        return self.steps_remaining == 0 or self.seconds_remaining == 0.0

    # ------------------------------------------------------------ use

    def check(self) -> None:
        """Raise if there is no room for another step.

        Called before each iteration. Time is checked first: a task that is
        both out of time and out of steps has really been stopped by the
        clock, and reporting ``MAX_STEPS`` would send someone looking at the
        wrong limit.
        """
        if self.elapsed_seconds >= self.max_seconds:
            raise BudgetExceeded(
                BudgetReason.MAX_TIME,
                f"Task exceeded {self.max_seconds:g}s.",
            )
        if self._steps_used >= self.max_steps:
            raise BudgetExceeded(
                BudgetReason.MAX_STEPS,
                f"Task exceeded {self.max_steps} steps.",
            )

    def consume_step(self) -> int:
        """Record a step and return its number, starting at 1.

        Separate from :meth:`check` because a step that never ran should not
        count: recovery retries within one step, and an approval that pauses
        the loop, must not silently eat the allowance.
        """
        self.check()
        self._steps_used += 1
        return self._steps_used

    def reset(self) -> None:
        """Start the clock again. Used when a replay falls back to planning."""
        self._started_at = self._clock()
        self._steps_used = 0
