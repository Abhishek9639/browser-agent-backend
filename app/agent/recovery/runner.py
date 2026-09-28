"""Running the recovery ladder (spec section 4.8).

Walks the ordered strategies until one of them produces a step the verifier
accepts, or the retry budget runs out. The budget is the point: without it a
seven rung ladder on a page that simply does not have the button turns one
failed step into seven failed steps and seven screenshots.

Which rung goes first depends on why the step failed; see ``routing``. The
budget is what makes that matter, because with three attempts to spend the
order decides which three rungs are ever reached.
"""

from typing import Final

from app.agent.contracts import Attempt, AttemptResult, RecoveryContext, Verdict
from app.agent.recovery.routing import order_for, route_for
from app.agent.recovery.strategies import ORDERED_STRATEGIES, RecoveryStrategy
from app.core import metrics
from app.core.logging import get_logger
from app.domain.enums import RecoveryOutcome

__all__ = ["RecoveryRunner", "failure_sentence"]

logger = get_logger(__name__)

# Rungs that were skipped without touching the page do not count against the
# budget: a strategy that could not apply has not cost a retry.
COUNTS_AGAINST_BUDGET: Final[frozenset[RecoveryOutcome]] = frozenset(
    {RecoveryOutcome.SUCCEEDED, RecoveryOutcome.FAILED}
)


class RecoveryRunner:
    """Tries the ordered strategies until one works or the budget is spent."""

    def __init__(
        self,
        *,
        max_attempts: int = 3,
        strategies: tuple[RecoveryStrategy, ...] = ORDERED_STRATEGIES,
    ) -> None:
        self._max_attempts = max_attempts
        self._strategies = strategies

    async def run(self, ctx: RecoveryContext) -> Verdict | None:
        """Climb the ladder. Returns the verdict that worked, or None.

        ``None`` means nothing was attempted or nothing worked, which the
        caller reads as "this step stays failed".

        One call is one recovery episode for the purposes of the section 9
        recovery rate, whatever it costs in attempts.
        """
        spent = 0
        ladder = order_for(self._strategies, ctx)
        rescued = False

        try:
            for strategy in ladder:
                if spent >= self._max_attempts:
                    logger.info("recovery_budget_spent", attempts=spent)
                    break
                if not strategy.applicable(ctx):
                    continue

                result = await self._try(strategy, ctx, number=len(ctx.attempts) + 1)
                if result.outcome in COUNTS_AGAINST_BUDGET:
                    spent += 1
                if result.succeeded:
                    logger.info(
                        "recovery_succeeded",
                        strategy=strategy.name,
                        attempts=spent,
                        # Whether the routing sent us here first, which is the
                        # number worth watching once this is in production.
                        routed=strategy.name in route_for(ctx)[:1],
                    )
                    rescued = True
                    return result.verdict

            return None
        finally:
            # One episode per call, whatever it cost in attempts, and recorded
            # even when no rung applied: a step that failed with nothing
            # applicable is still a failure that was not recovered.
            metrics.observe_recovery_episode(rescued=rescued)

    async def _try(
        self, strategy: RecoveryStrategy, ctx: RecoveryContext, *, number: int
    ) -> AttemptResult:
        """Run one rung and record it.

        Numbered by how many have been recorded, not by how much budget has
        been spent: a skipped rung is still an attempt in the log, and two
        rows sharing an attempt number would make the step's history
        unorderable.
        """
        try:
            result = await strategy.attempt(ctx)
        except Exception as exc:
            # A strategy that throws must not take the ladder down with it:
            # the next rung may well be the one that works.
            logger.warning(
                "recovery_strategy_failed", strategy=strategy.name, error_type=type(exc).__name__
            )
            result = AttemptResult(RecoveryOutcome.FAILED, error=type(exc).__name__)

        metrics.recovery_attempts_total.labels(
            strategy=strategy.name,
            outcome=result.outcome.value,
        ).inc()
        ctx.tried.append(strategy.label)
        attempt = Attempt(
            number=number,
            strategy=strategy.name,
            label=strategy.label,
            outcome=result.outcome,
            error=result.error,
        )
        ctx.attempts.append(attempt)

        if ctx.on_attempt is not None:
            await ctx.on_attempt(attempt)

        return result


def failure_sentence(ctx: RecoveryContext, tried: list[str]) -> str:
    """What to tell a person when the ladder ran out.

    Names the action, the target and what was tried, because "step 3 failed"
    tells a user nothing they can act on and "could not click Search flights,
    tried re-reading the page, re-locating by name and the stored selector"
    tells them the button is probably not there any more.
    """
    action = ctx.decision.action.value.replace("_", " ")
    target = ctx.decision.target or (ctx.element.name if ctx.element else None)
    named = f" {target!r}" if target else ""

    if not tried:
        return f"Could not {action}{named}."
    return f"Could not {action}{named}: tried {_join(tried)}."


def _join(items: list[str]) -> str:
    seen = list(dict.fromkeys(items))
    if len(seen) == 1:
        return seen[0]
    return f"{', '.join(seen[:-1])} and {seen[-1]}"
