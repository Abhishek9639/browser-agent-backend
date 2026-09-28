"""Prometheus metrics (spec sections 6 and 9).

Section 9 names the KPIs this project is judged on: completion rate, recovery
rate, verification accuracy, median time per task, cache hit rate, approval
rate, cost per task, tokens, LLM latency and vision invocation rate. Every one
of them is derivable from what is defined here.

Three rules hold the module together.

**One registry, defined once.** The collectors are module-level, because
Prometheus client objects are process-wide by nature and registering the same
name twice raises. Tests that need a clean slate call :func:`reset`.

**No unbounded labels.** A label whose values come from user input is a memory
leak with a graph attached. HTTP metrics are labelled by the route *template*,
never the path, so ``/tasks/{task_id}`` is one series rather than one per task.
Nothing here is ever labelled by task id, user id or URL.

**Counters, not derived values.** Ratios belong in PromQL, where the window is
the question being asked rather than whatever this process has seen since it
started. The one exception is ``recovery_success_ratio``, which section 9 names
as a KPI and which the stats endpoint also reports; it is maintained from two
counters kept beside it and documented as a convenience.
"""

from typing import Final

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

__all__ = [
    "CONTENT_TYPE",
    "REGISTRY",
    "active_tasks",
    "approvals_total",
    "cache_lookups_total",
    "http_errors_total",
    "http_request_duration_seconds",
    "llm_errors_total",
    "llm_request_duration_seconds",
    "llm_tokens_total",
    "observe_recovery_episode",
    "queued_tasks",
    "recovery_attempts_total",
    "recovery_success_ratio",
    "render",
    "reset",
    "steps_per_task",
    "task_duration_seconds",
    "tasks_total",
    "verification_total",
    "vision_invocations_total",
    "ws_delivery_latency_seconds",
]

CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"

# Our own registry rather than the global default, so the process collectors
# prometheus_client installs by default are not mixed into a deliberately
# small exposition, and so a test can inspect exactly what we publish.
REGISTRY: Final = CollectorRegistry()

# Buckets in seconds. The API target is a p95 under two seconds excluding agent
# work (spec section 9), so the resolution is where that decision is made.
_API_BUCKETS: Final = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0)
# A task is minutes, not milliseconds. The default budget is 300 seconds.
_TASK_BUCKETS: Final = (1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0, 600.0)
# An LLM call is a second or two on a good day and a timeout on a bad one.
_LLM_BUCKETS: Final = (0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0)
# The live view target is one to two seconds after the action.
_WS_BUCKETS: Final = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0)
_STEP_BUCKETS: Final = (1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 18.0, 25.0, 40.0)


# ----------------------------------------------------------------- the API

http_request_duration_seconds: Final = Histogram(
    "http_request_duration_seconds",
    "How long an API request took, by route template.",
    labelnames=("route", "method", "status"),
    buckets=_API_BUCKETS,
    registry=REGISTRY,
)

http_errors_total: Final = Counter(
    "http_errors_total",
    "Requests that returned 4xx or 5xx.",
    labelnames=("route", "method", "status"),
    registry=REGISTRY,
)


# ---------------------------------------------------------------- the agent

tasks_total: Final = Counter(
    "tasks_total",
    "Tasks that reached a terminal state, by that state.",
    labelnames=("status",),
    registry=REGISTRY,
)

task_duration_seconds: Final = Histogram(
    "task_duration_seconds",
    "Wall clock from the start of a run to its terminal state.",
    labelnames=("status",),
    buckets=_TASK_BUCKETS,
    registry=REGISTRY,
)

steps_per_task: Final = Histogram(
    "steps_per_task",
    "Steps a task consumed before it stopped.",
    labelnames=("status",),
    buckets=_STEP_BUCKETS,
    registry=REGISTRY,
)

verification_total: Final = Counter(
    "verification_total",
    "Deterministic verification outcomes, by result and by the check that ran.",
    labelnames=("result", "method"),
    registry=REGISTRY,
)

recovery_attempts_total: Final = Counter(
    "recovery_attempts_total",
    "Recovery ladder attempts, by strategy and outcome.",
    labelnames=("strategy", "outcome"),
    registry=REGISTRY,
)

recovery_success_ratio: Final = Gauge(
    "recovery_success_ratio",
    "Share of failed steps this process rescued. Section 9 KPI; prefer the "
    "counters below it for any other window.",
    registry=REGISTRY,
)

recovery_episodes_total: Final = Counter(
    "recovery_episodes_total",
    "Steps that needed rescuing, whether or not they were rescued.",
    registry=REGISTRY,
)

recovery_rescued_total: Final = Counter(
    "recovery_rescued_total",
    "Steps the ladder rescued.",
    registry=REGISTRY,
)

vision_invocations_total: Final = Counter(
    "vision_invocations_total",
    "Visual grounding calls, by what triggered them and how they ended.",
    labelnames=("reason", "outcome"),
    registry=REGISTRY,
)

approvals_total: Final = Counter(
    "approvals_total",
    "Approval requests that were resolved, by outcome.",
    labelnames=("outcome",),
    registry=REGISTRY,
)

cache_lookups_total: Final = Counter(
    "cache_lookups_total",
    "Flow cache lookups, by whether a usable flow was found.",
    labelnames=("result",),
    registry=REGISTRY,
)


# ------------------------------------------------------------------- the LLM

llm_request_duration_seconds: Final = Histogram(
    "llm_request_duration_seconds",
    "Latency of one model call, by provider and model.",
    labelnames=("provider", "model", "purpose"),
    buckets=_LLM_BUCKETS,
    registry=REGISTRY,
)

llm_tokens_total: Final = Counter(
    "llm_tokens_total",
    "Tokens billed, by direction. Counts, never content.",
    labelnames=("provider", "model", "direction"),
    registry=REGISTRY,
)

llm_errors_total: Final = Counter(
    "llm_errors_total",
    "Model calls that raised, by provider and exception type.",
    labelnames=("provider", "model", "error"),
    registry=REGISTRY,
)


# ------------------------------------------------------------- the live view

ws_delivery_latency_seconds: Final = Histogram(
    "ws_delivery_latency_seconds",
    "From the bus stamping a step event to a client being sent it.",
    buckets=_WS_BUCKETS,
    registry=REGISTRY,
)


# ----------------------------------------------------------------- capacity

active_tasks: Final = Gauge(
    "active_tasks",
    "Tasks running in this process right now.",
    registry=REGISTRY,
)

queued_tasks: Final = Gauge(
    "queued_tasks",
    "Tasks accepted by this process and waiting for a concurrency slot.",
    registry=REGISTRY,
)


# ------------------------------------------------------------------ helpers


def observe_recovery_episode(*, rescued: bool) -> None:
    """Record one failed step and whether the ladder saved it.

    Per failed step, not per attempt. Spec section 9 asks for recovery success
    on induced failures, and counting attempts answers a different question:
    a step rescued on the third rung is one success, not one success and two
    failures. Getting this wrong once already produced a recovery rate of 36%
    for a run that actually rescued 80% of what broke.
    """
    recovery_episodes_total.inc()
    if rescued:
        recovery_rescued_total.inc()

    episodes = _value(recovery_episodes_total)
    rescues = _value(recovery_rescued_total)
    recovery_success_ratio.set(rescues / episodes if episodes else 0.0)


def render() -> bytes:
    """The exposition text for ``GET /metrics``."""
    return generate_latest(REGISTRY)


def reset() -> None:
    """Forget every sample. For tests only.

    Prometheus collectors are process-wide, so a test that asserts on a counter
    has to be able to start from zero without re-importing the module.

    ``clear()`` only empties the label map, which is everything for a labelled
    collector and nothing at all for an unlabelled one: those keep their value
    in a child of their own. Missing that made one test's recovery episodes turn
    up in the next test's total, which is the sort of pollution that teaches
    people to distrust a suite.
    """
    for collector in list(REGISTRY._collector_to_names):
        clear = getattr(collector, "clear", None)
        if callable(clear):
            clear()
        _zero(collector)


def _zero(collector: object) -> None:
    """Zero an unlabelled collector's own value. Test support only."""
    for attribute in ("_value", "_sum"):
        holder = getattr(collector, attribute, None)
        if holder is not None and hasattr(holder, "set"):
            holder.set(0)

    for bucket in getattr(collector, "_buckets", None) or ():
        if hasattr(bucket, "set"):
            bucket.set(0)


def _value(counter: Counter) -> float:
    """The current total of an unlabelled counter.

    prometheus_client has no public reader for this. Collecting the one metric
    is the documented way round it and costs nothing at the rate this is
    called, which is once per rescued step.
    """
    for metric in counter.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total"):
                return float(sample.value)
    return 0.0
