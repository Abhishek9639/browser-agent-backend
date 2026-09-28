#!/usr/bin/env python
"""Measure the agent against the spec section 9 targets.

Runs the primary task repeatedly against SkyFare, once per chaos configuration
per round, and writes a dated report. The numbers it reports are the ones the
spec asks for, and each is marked against its target so the report answers
"are we there yet" rather than leaving that to the reader.

    uv run python scripts/benchmark.py --runs 3
    uv run python scripts/benchmark.py --runs 5 --provider gemini --chaos none,cookie

Defaults to the mock provider. A benchmark that spends money by default is a
benchmark nobody runs twice.
"""

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401  (registers every table on Base.metadata)
from app.agent.cache.store import DatabaseFlowCache
from app.agent.llm.factory import build_llm
from app.agent.llm.mock import MockLLM
from app.core.config import Settings
from app.db.base import Base
from app.models.cache import CachedFlow
from tests.chaos.harness import (
    CHAOS_CONFIGS,
    RunReport,
    make_task,
    run_once,
    session_unit_of_work,
    skyfare_server,
)

REPORT_DIR = Path("docs/benchmarks")

# Spec section 9. A target of None means the metric is reported but has no
# published bar to clear.
TARGETS: dict[str, float | None] = {
    "success_rate": 0.80,
    "recovery_rate": 0.70,
    "verification_accuracy": None,
    "median_seconds": None,
    "p90_seconds": None,
}


@dataclass
class ConfigResult:
    """Every run of one chaos configuration."""

    chaos: str
    runs: list[RunReport] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.runs)

    @property
    def successes(self) -> int:
        return sum(1 for run in self.runs if run.correct)

    @property
    def success_rate(self) -> float:
        return self.successes / self.total if self.total else 0.0

    @property
    def clean(self) -> int:
        """Runs that either answered correctly or failed readably.

        Reported separately from the success rate because they are different
        questions: one is "did it work", the other is "did it behave".
        """
        return sum(1 for run in self.runs if run.correct or run.clean_failure)

    @property
    def attempts(self) -> int:
        return sum(len(run.attempts) for run in self.runs)

    @property
    def episodes(self) -> int:
        return sum(len(run.episodes) for run in self.runs)

    @property
    def recoveries(self) -> int:
        return sum(run.recovered_episodes for run in self.runs)

    @property
    def steps(self) -> list[int]:
        return [len(run.steps) for run in self.runs]

    @property
    def seconds(self) -> list[float]:
        return [run.wall_ms / 1000 for run in self.runs]

    @property
    def verified_steps(self) -> int:
        return sum(run.verified_steps for run in self.runs)

    @property
    def total_steps(self) -> int:
        return sum(len(run.steps) for run in self.runs)

    @property
    def vision_calls(self) -> int:
        return sum(run.outcome.vision_calls for run in self.runs)

    @property
    def tokens_in(self) -> int:
        return sum(run.outcome.vision_tokens for run in self.runs)


def summarise(results: list[ConfigResult], settings: Settings) -> dict[str, Any]:
    """The whole run, as numbers."""
    runs = [run for result in results for run in result.runs]
    total = len(runs)
    seconds = sorted(run.wall_ms / 1000 for run in runs)
    attempts = sum(len(run.attempts) for run in runs)
    episodes = sum(len(run.episodes) for run in runs)
    recoveries = sum(run.recovered_episodes for run in runs)
    steps_total = sum(len(run.steps) for run in runs)
    verified = sum(run.verified_steps for run in runs)
    vision_calls = sum(run.outcome.vision_calls for run in runs)
    vision_tokens = sum(run.outcome.vision_tokens for run in runs)

    return {
        "runs": total,
        "success_rate": (sum(1 for run in runs if run.correct) / total) if total else 0.0,
        "clean_rate": (sum(1 for run in runs if run.correct or run.clean_failure) / total)
        if total
        else 0.0,
        # Per failure, not per attempt: the ladder tries several rungs to fix
        # one failure, and scoring the rungs would call a step rescued on the
        # third try two failures and one success.
        "recovery_rate": (recoveries / episodes) if episodes else None,
        "recovery_episodes": episodes,
        "recovery_attempts": attempts,
        "verification_accuracy": (verified / steps_total) if steps_total else None,
        "median_seconds": statistics.median(seconds) if seconds else 0.0,
        "p90_seconds": _percentile(seconds, 0.9),
        "avg_steps": (steps_total / total) if total else 0.0,
        "vision_invocation_rate": (vision_calls / steps_total) if steps_total else 0.0,
        "vision_calls": vision_calls,
        "approvals": 0,
        "cache_hit_rate": (sum(1 for run in runs if run.outcome.cache_hit) / total)
        if total
        else 0.0,
        "tokens_in": vision_tokens,
        "tokens_out": 0,
        "estimated_cost": _cost(vision_tokens, 0, settings),
        "currency": settings.LLM_PRICE_CURRENCY,
    }


def _percentile(ordered: Sequence[float], fraction: float) -> float:
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _cost(tokens_in: int, tokens_out: int, settings: Settings) -> float:
    """Cost from the configured prices, never from a number typed in here."""
    million = 1_000_000
    return (
        tokens_in / million * settings.LLM_PRICE_INPUT_PER_MTOK
        + tokens_out / million * settings.LLM_PRICE_OUTPUT_PER_MTOK
    )


def verdict(name: str, value: float | None) -> str:
    target = TARGETS.get(name)
    if target is None:
        return "—"
    if value is None:
        return "no data"
    return "PASS" if value >= target else "FAIL"


def render(summary: dict[str, Any], results: list[ConfigResult], meta: dict[str, Any]) -> str:
    """The report, as a page somebody can read without running anything."""
    lines = [
        f"# Benchmark {meta['date']} ({meta['provider']})",
        "",
        f"{summary['runs']} runs, {meta['runs_per_config']} per configuration, "
        f"against SkyFare on {meta['machine']}.",
        "",
        "## Against the targets",
        "",
        "| Metric | Result | Target | |",
        "| --- | --- | --- | --- |",
        f"| End-to-end success | {summary['success_rate']:.0%} | ≥80% | "
        f"{verdict('success_rate', summary['success_rate'])} |",
        f"| Recovery success | {_pct(summary['recovery_rate'])} | >70% | "
        f"{verdict('recovery_rate', summary['recovery_rate'])} |",
        f"| Verification accuracy | {_pct(summary['verification_accuracy'])} | — | — |",
        f"| Median time per task | {summary['median_seconds']:.1f}s | — | — |",
        f"| p90 time per task | {summary['p90_seconds']:.1f}s | — | — |",
        "",
        "Success means the task completed *and* reported the fare the oracle "
        "says is cheapest. A run that completes with the wrong number counts "
        "as a failure, because a confident wrong answer is the one outcome a "
        "user cannot detect.",
        "",
        "## Everything else",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Ran without crashing | {summary['clean_rate']:.0%} |",
        f"| Average steps per task | {summary['avg_steps']:.1f} |",
        f"| Steps that needed rescuing | {summary['recovery_episodes']} |",
        f"| Recovery attempts made | {summary['recovery_attempts']} |",
        f"| Vision invocation rate | {summary['vision_invocation_rate']:.2f} per step |",
        f"| Approvals requested | {summary['approvals']} |",
        f"| Cache hit rate | {summary['cache_hit_rate']:.0%} |",
        f"| Tokens in / out | {summary['tokens_in']} / {summary['tokens_out']} |",
        f"| Estimated cost | {summary['estimated_cost']:.4f} {summary['currency']} |",
        "",
        "## By configuration",
        "",
        "| Chaos | Runs | Correct | Clean | Needed rescue | Rescued | Attempts | Median | Steps |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    for result in results:
        median = statistics.median(result.seconds) if result.seconds else 0.0
        avg_steps = (sum(result.steps) / result.total) if result.total else 0.0
        lines.append(
            f"| `{result.chaos}` | {result.total} | {result.successes} | {result.clean} "
            f"| {result.episodes} | {result.recoveries} | {result.attempts} "
            f"| {median:.1f}s | {avg_steps:.1f} |"
        )

    lines += [
        "",
        "## How to read this",
        "",
        f"Provider `{meta['provider']}`, prices "
        f"{meta['price_in']}/{meta['price_out']} {summary['currency']} per million tokens "
        "in and out. Cost is derived from those settings rather than from "
        "anything written into the script.",
        "",
        meta["caveat"],
        "",
    ]
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    return "no data" if value is None else f"{value:.0%}"


async def main(args: argparse.Namespace) -> int:
    settings = Settings()
    provider = args.provider or ("mock" if not args.live else settings.LLM_PROVIDER)
    llm = MockLLM() if provider == "mock" else build_llm(settings, provider=provider)

    configs = [c.strip() for c in args.chaos.split(",")] if args.chaos else list(CHAOS_CONFIGS)
    configs = ["" if c in {"none", ""} else c for c in configs]

    # Its own database rather than the dev one: a benchmark writes hundreds of
    # task rows and they have no business sitting alongside real work. The
    # schema is created here because nothing else creates it outside pytest.
    engine = create_async_engine(settings.test_database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    results = [ConfigResult(chaos=c or "none") for c in configs]

    if args.cache:
        # Start from nothing, so the first round of a cached benchmark is
        # always the round that fills the cache. Left-over flows from an
        # earlier run would make the hit rate depend on what was measured
        # yesterday.
        async with maker() as session:
            await session.execute(delete(CachedFlow))
            await session.commit()

    storage = Path(args.storage)
    storage.mkdir(parents=True, exist_ok=True)

    with skyfare_server() as base:
        for round_number in range(args.runs):
            for config, result in zip(configs, results, strict=True):
                label = config or "none"
                print(f"run {round_number + 1}/{args.runs}  {label}", flush=True)
                async with maker() as session:
                    cache = (
                        DatabaseFlowCache(
                            session_unit_of_work(session),
                            min_success_rate=settings.CACHE_MIN_SUCCESS_RATE,
                        )
                        if args.cache
                        else None
                    )
                    report = await _one(session, config, base, settings, storage, llm, cache)
                result.runs.append(report)
                print(
                    f"    {report.outcome.status.value}"
                    f"{'  correct' if report.correct else ''}"
                    f"  {report.wall_ms / 1000:.1f}s"
                    f"  {len(report.steps)} steps"
                    f"  {len(report.attempts)} attempts",
                    flush=True,
                )

    await engine.dispose()

    summary = summarise(results, settings)
    meta = {
        "date": date.today().isoformat(),
        "provider": provider,
        "runs_per_config": args.runs,
        "cache": bool(args.cache),
        "machine": sys.platform,
        "price_in": settings.LLM_PRICE_INPUT_PER_MTOK,
        "price_out": settings.LLM_PRICE_OUTPUT_PER_MTOK,
        "caveat": (
            "The mock provider is rule-based, so these numbers measure the loop, "
            "the verifier and the recovery ladder rather than a model's judgement. "
            "Vision always declines under the mock, which is the correct "
            "degradation path but makes the vision figures meaningless here."
            if provider == "mock"
            else "Run against a real provider, so model latency and cost are real."
        ),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    # A cached run measures something different, so it gets its own file
    # rather than overwriting the comparable series.
    stem = f"{meta['date']}-{provider}{'-cache' if args.cache else ''}"
    (REPORT_DIR / f"{stem}.md").write_text(render(summary, results, meta), encoding="utf-8")
    (REPORT_DIR / f"{stem}.json").write_text(
        json.dumps(
            {
                "meta": meta,
                "summary": summary,
                "by_config": [
                    {
                        "chaos": result.chaos,
                        "runs": result.total,
                        "correct": result.successes,
                        "clean": result.clean,
                        "attempts": result.attempts,
                        "recovered": result.recoveries,
                        "seconds": result.seconds,
                        "steps": result.steps,
                    }
                    for result in results
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"\nwrote {REPORT_DIR / f'{stem}.md'}")
    print(
        f"success {summary['success_rate']:.0%} "
        f"({verdict('success_rate', summary['success_rate'])}), "
        f"recovery {_pct(summary['recovery_rate'])} "
        f"({verdict('recovery_rate', summary['recovery_rate'])})"
    )
    return 0


async def _one(
    session: AsyncSession,
    config: str,
    base: str,
    settings: Settings,
    storage: Path,
    llm: Any,
    cache: Any = None,
) -> RunReport:
    task = await make_task(session, email=f"bench-{uuid.uuid4().hex[:10]}@example.test")
    try:
        return await run_once(
            chaos=config,
            base_url=base,
            session=session,
            settings=settings,
            storage_dir=storage,
            llm=llm,
            task=task,
            cache=cache,
        )
    finally:
        await session.rollback()


def parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark the agent against SkyFare.")
    parser.add_argument("--runs", type=int, default=1, help="Rounds per configuration.")
    parser.add_argument("--provider", default=None, help="mock, anthropic, openai, gemini.")
    parser.add_argument("--live", action="store_true", help="Use the configured provider.")
    parser.add_argument("--chaos", default=None, help="Comma-separated configs, or all.")
    parser.add_argument(
        "--cache",
        action="store_true",
        help=(
            "Let runs replay each other's flows. Off by default: a replayed run "
            "does not exercise the planner, so the success and recovery numbers "
            "would stop being comparable with every previous benchmark."
        ),
    )
    parser.add_argument(
        "--storage",
        default=str(Path(tempfile.gettempdir()) / "agent-benchmark"),
        help="Where to write screenshots.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse())))
