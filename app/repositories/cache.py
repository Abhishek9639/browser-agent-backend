"""Cached flow repository (spec section 4.12)."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models.cache import CachedFlow
from app.repositories.base import BaseRepository

__all__ = ["CachedFlowRepository"]


class CachedFlowRepository(BaseRepository[CachedFlow]):
    model = CachedFlow

    async def get_by_signature(self, task_signature: str) -> CachedFlow | None:
        """Exact-signature lookup. Semantic matching is out of scope for MVP."""
        stmt = select(CachedFlow).where(CachedFlow.task_signature == task_signature)
        flow: CachedFlow | None = await self.session.scalar(stmt)
        return flow

    async def upsert(
        self,
        task_signature: str,
        action_sequence: list[dict[str, Any]],
        *,
        success_rate: float = 1.0,
        run_count: int = 1,
    ) -> CachedFlow:
        """Insert, or overwrite the sequence for an existing signature.

        Uses ``ON CONFLICT`` rather than a read-then-write so two tasks
        finishing the same flow at once cannot race into a unique violation.
        """
        stmt = (
            insert(CachedFlow)
            .values(
                task_signature=task_signature,
                action_sequence=action_sequence,
                success_rate=success_rate,
                run_count=run_count,
            )
            .on_conflict_do_update(
                index_elements=[CachedFlow.task_signature],
                set_={
                    "action_sequence": action_sequence,
                    "success_rate": success_rate,
                    "run_count": run_count,
                },
            )
            .returning(CachedFlow)
        )
        # populate_existing: without it the identity map hands back the instance
        # already in this session, still carrying its pre-upsert values, even
        # though RETURNING gave us the new ones.
        result = await self.session.scalars(stmt.execution_options(populate_existing=True))
        flow: CachedFlow = result.one()
        await self.session.flush()
        return flow

    async def record_run(self, flow: CachedFlow, *, succeeded: bool) -> CachedFlow:
        """Fold one replay outcome into the running success rate.

        A run is any replay attempt; success means every step verified with no
        planner fallback (decision A14).
        """
        previous_successes = flow.success_rate * flow.run_count
        flow.run_count += 1
        flow.success_rate = (previous_successes + (1.0 if succeeded else 0.0)) / flow.run_count
        await self.session.flush()
        return flow

    async def should_invalidate(self, flow: CachedFlow) -> bool:
        """Spec section 4.12: below 0.5 after at least three runs."""
        return flow.run_count >= 3 and flow.success_rate < 0.5
