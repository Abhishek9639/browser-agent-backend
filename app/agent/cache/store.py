"""The flow cache, backed by ``cached_flows`` (spec section 4.12).

Owns the four things the spec asks of a cache and nothing else: what the
signature is, whether a stored flow is good enough to replay, what to store
when a task succeeds, and what to do with a flow that keeps failing.

Deliberately not in ``app/services/``: the orchestrator drives it, and
``app/agent/`` may not import FastAPI, which is the line that keeps the loop
testable without an HTTP stack. It takes a unit-of-work factory for the same
reason the rest of the loop does, so it never holds a session between calls.
"""

import uuid

from app.agent.cache.flow import ReplayStep, decode_steps, encode_steps
from app.agent.cache.signature import task_signature
from app.agent.contracts import ReplayPlan
from app.agent.orchestrator import UnitOfWorkFactory
from app.core.logging import get_logger
from app.services.events import EventBus

__all__ = ["DatabaseFlowCache"]

logger = get_logger(__name__)

# Below this many runs the success rate is an opinion rather than a
# measurement, so a flow is not thrown away on one bad day (spec section 4.12).
MIN_RUNS_BEFORE_INVALIDATION = 3


class DatabaseFlowCache:
    """An :class:`app.agent.contracts.FlowCache` over the database."""

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        *,
        min_success_rate: float = 0.5,
        enabled: bool = True,
        events: EventBus | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._min_success_rate = min_success_rate
        self._enabled = enabled
        self._events = events

    # --------------------------------------------------------------- lookup

    async def plan(self, task_id: uuid.UUID, instruction: str) -> ReplayPlan | None:
        """The flow this task should replay, if any.

        A task that already names a flow is a forced replay: that is what the
        replay endpoint does, and it deliberately ignores the success rate,
        because a user re-running their own task has asked for this flow rather
        than for the cache's opinion of it.
        """
        if not self._enabled:
            return None

        async with self._unit_of_work() as uow:
            task = await uow.tasks.get_or_404(task_id)
            forced = task.cache_flow_id is not None

            flow = (
                await uow.cached_flows.get(task.cache_flow_id)
                if task.cache_flow_id is not None
                else await uow.cached_flows.get_by_signature(
                    task_signature(instruction, task.start_url)
                )
            )
            if flow is None:
                return None

            eligible = forced or flow.success_rate >= self._min_success_rate
            steps = decode_steps(flow.action_sequence)
            if not eligible or not steps:
                logger.info(
                    "cache_flow_skipped",
                    flow_id=str(flow.id),
                    success_rate=round(flow.success_rate, 3),
                    steps=len(steps),
                )
                return None

            # Bind the task to the flow now rather than at the end. A run that
            # dies mid-replay should still be traceable to what it replayed.
            task.cache_flow_id = flow.id
            plan = ReplayPlan(
                flow_id=flow.id,
                steps=steps,
                run_count=flow.run_count,
                success_rate=flow.success_rate,
                forced=forced,
            )

        logger.info(
            "cache_hit",
            task_id=str(task_id),
            flow_id=str(plan.flow_id),
            steps=len(plan.steps),
            forced=forced,
            success_rate=round(plan.success_rate, 3),
        )
        return plan

    # ---------------------------------------------------------------- write

    async def save(
        self,
        task_id: uuid.UUID,
        instruction: str,
        steps: tuple[ReplayStep, ...],
    ) -> None:
        """Remember the sequence a completed task verified.

        Called only on COMPLETED, and only with steps the verifier passed. A
        flow containing a step that did not work is worse than no flow: it
        would be replayed, fail, and cost the run a fallback for nothing.
        """
        if not self._enabled or not steps:
            return

        async with self._unit_of_work() as uow:
            task = await uow.tasks.get_or_404(task_id)
            signature = task_signature(instruction, task.start_url)

            existing = await uow.cached_flows.get_by_signature(signature)
            # A first store starts at one run and a full success rate, because
            # it is exactly one run and it worked. Re-storing over a known flow
            # keeps the statistics: the sequence may have improved, but its
            # history of replays is what decides whether it is used.
            flow = await uow.cached_flows.upsert(
                signature,
                encode_steps(steps),
                success_rate=existing.success_rate if existing else 1.0,
                run_count=existing.run_count if existing else 1,
            )
            task.cache_flow_id = flow.id

        logger.info(
            "cache_flow_stored",
            task_id=str(task_id),
            flow_id=str(flow.id),
            steps=len(steps),
            replaced=existing is not None,
        )

    async def settle(self, plan: ReplayPlan, *, replayed_all: bool) -> None:
        """Fold one replay into the flow's statistics, and invalidate if due.

        A run counts whether or not it finished the flow. Success means every
        cached step replayed and verified with no fallback to the planner,
        which is a stricter reading than "the task completed": a flow that
        needs rescuing every time is not a flow worth replaying, even when the
        task around it succeeds.
        """
        async with self._unit_of_work() as uow:
            flow = await uow.cached_flows.get(plan.flow_id)
            if flow is None:
                return

            await uow.cached_flows.record_run(flow, succeeded=replayed_all)
            rate, runs = flow.success_rate, flow.run_count

            if await uow.cached_flows.should_invalidate(flow):
                await uow.cached_flows.delete(flow)
                logger.info(
                    "cache_flow_invalidated",
                    flow_id=str(plan.flow_id),
                    success_rate=round(rate, 3),
                    run_count=runs,
                )
                return

        logger.info(
            "cache_flow_run_recorded",
            flow_id=str(plan.flow_id),
            replayed_all=replayed_all,
            success_rate=round(rate, 3),
            run_count=runs,
        )
