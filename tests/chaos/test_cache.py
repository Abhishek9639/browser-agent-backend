"""Replay against the real site (spec section 4.12, TEST-009).

The cache is the one feature whose whole point is what it does the *second*
time, so these tests run the same task twice and compare. Against a real
Chromium and real SkyFare, because replay is re-resolving stored locators on a
live page and a fake page cannot go stale.

Marked `chaos` and not part of `make check` for the same reason as the rest of
this directory: minutes, not seconds.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.cache.signature import task_signature
from app.agent.cache.store import DatabaseFlowCache
from app.agent.llm.mock import MockLLM
from app.core.config import Settings
from app.domain.enums import TaskStatus
from app.models.task import LLMDecision
from app.repositories.unit_of_work import UnitOfWork
from tests.chaos.harness import RunReport, make_task, run_once, session_unit_of_work

pytestmark = pytest.mark.chaos


def uow_factory(session: AsyncSession) -> Any:
    """A unit of work that does not commit, for reading between runs."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[UnitOfWork]:
        yield UnitOfWork(session)

    return factory


async def run(
    *,
    skyfare: str,
    session: AsyncSession,
    settings: Settings,
    storage_dir: Path,
    cache: DatabaseFlowCache,
    llm: MockLLM,
) -> RunReport:
    task = await make_task(session)
    return await run_once(
        chaos="",
        base_url=skyfare,
        session=session,
        settings=settings,
        storage_dir=storage_dir,
        llm=llm,
        task=task,
        cache=cache,
    )


async def test_the_same_task_twice_replays_and_costs_fewer_calls(
    skyfare: str,
    chaos_session: AsyncSession,
    chaos_settings: Settings,
    tmp_path: Path,
) -> None:
    """TEST-009.

    Two identical tasks. The first teaches the cache; the second replays what
    it learned and only asks the model for the answer, which is the one thing
    that is never cached.
    """
    cache = DatabaseFlowCache(session_unit_of_work(chaos_session))

    first_llm = MockLLM()
    first = await run(
        skyfare=skyfare,
        session=chaos_session,
        settings=chaos_settings,
        storage_dir=tmp_path,
        cache=cache,
        llm=first_llm,
    )
    assert first.outcome.status is TaskStatus.COMPLETED, first.outcome.failure_reason
    assert first.correct, first.answer
    assert first.outcome.cache_hit is False

    second_llm = MockLLM()
    second = await run(
        skyfare=skyfare,
        session=chaos_session,
        settings=chaos_settings,
        storage_dir=tmp_path,
        cache=cache,
        llm=second_llm,
    )

    assert second.outcome.status is TaskStatus.COMPLETED, second.outcome.failure_reason
    assert second.outcome.cache_hit is True
    assert second.outcome.replayed_steps > 0
    # The saving, and the reason the feature exists.
    assert second_llm.call_count < first_llm.call_count
    # Still the right answer, and read from today's page rather than recalled.
    assert second.correct, second.answer


async def test_a_replayed_run_records_its_steps_without_a_model_behind_them(
    skyfare: str,
    chaos_session: AsyncSession,
    chaos_settings: Settings,
    tmp_path: Path,
) -> None:
    """A replayed step has no ``llm_decisions`` row, which is the audit trail.

    Reading it back is how the saving is provable after the fact rather than
    only observable while the run is happening.
    """
    cache = DatabaseFlowCache(session_unit_of_work(chaos_session))
    await run(
        skyfare=skyfare,
        session=chaos_session,
        settings=chaos_settings,
        storage_dir=tmp_path,
        cache=cache,
        llm=MockLLM(),
    )
    second = await run(
        skyfare=skyfare,
        session=chaos_session,
        settings=chaos_settings,
        storage_dir=tmp_path,
        cache=cache,
        llm=MockLLM(),
    )

    assert second.task_id is not None
    uow = UnitOfWork(chaos_session)
    steps = await uow.task_steps.list_for_task(second.task_id)
    decisions = await chaos_session.scalar(
        select(func.count())
        .select_from(LLMDecision)
        .where(LLMDecision.step_id.in_([step.id for step in steps]))
    )

    assert decisions is not None
    assert len(steps) > decisions
    assert decisions >= 1  # the final answer is always the model's


async def test_a_stale_flow_falls_back_and_loses_its_success_rate(
    skyfare: str,
    chaos_session: AsyncSession,
    chaos_settings: Settings,
    tmp_path: Path,
) -> None:
    """A flow stored against one page, replayed against a different one.

    The chaos configuration changes the control names, so the stored locators
    stop resolving. The run must still complete, and the flow must be marked
    down for it: a sequence that needs rescuing is not one worth replaying.
    """
    cache = DatabaseFlowCache(session_unit_of_work(chaos_session))
    await run(
        skyfare=skyfare,
        session=chaos_session,
        settings=chaos_settings,
        storage_dir=tmp_path,
        cache=cache,
        llm=MockLLM(),
    )

    uow = UnitOfWork(chaos_session)
    task = await make_task(chaos_session)
    signature = task_signature(task.instruction, task.start_url)
    flow = await uow.cached_flows.get_by_signature(signature)
    assert flow is not None
    before = flow.success_rate

    # Same instruction, a page that has moved everything.
    stale = await run_once(
        chaos="shuffle,layoutb",
        base_url=skyfare,
        session=chaos_session,
        settings=chaos_settings,
        storage_dir=tmp_path,
        llm=MockLLM(),
        task=task,
        cache=cache,
    )

    assert stale.outcome.status is TaskStatus.COMPLETED, stale.outcome.failure_reason
    refreshed = await uow.cached_flows.get_by_signature(signature)
    if refreshed is not None:
        assert refreshed.run_count > 1
        assert refreshed.success_rate <= before
