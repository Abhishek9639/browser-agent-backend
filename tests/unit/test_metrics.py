"""The metrics registry itself (spec sections 6 and 9).

Two things worth testing without a database or an app: that the exposition
contains what section 9 asks for, and that the label sets cannot grow without
bound. The second is the one that takes a service down.
"""

import pytest

from app.core import metrics


@pytest.fixture(autouse=True)
def clean() -> None:
    """Collectors are process-wide, so each test starts from zero."""
    metrics.reset()


def exposition() -> str:
    return metrics.render().decode()


def test_every_kpi_in_section_nine_has_a_metric() -> None:
    """Completion, recovery, verification, duration, cache, approvals, cost,
    tokens, LLM latency and vision rate. Named here so a rename has to be
    deliberate rather than silent.
    """
    metrics.tasks_total.labels(status="COMPLETED").inc()
    metrics.task_duration_seconds.labels(status="COMPLETED").observe(12.5)
    metrics.steps_per_task.labels(status="COMPLETED").observe(7)
    metrics.verification_total.labels(result="passed", method="dom_changed").inc()
    metrics.recovery_attempts_total.labels(strategy="dismiss_overlay", outcome="SUCCEEDED").inc()
    metrics.observe_recovery_episode(rescued=True)
    metrics.cache_lookups_total.labels(result="hit").inc()
    metrics.approvals_total.labels(outcome="APPROVED").inc()
    metrics.vision_invocations_total.labels(reason="duplicate_names", outcome="resolved").inc()
    metrics.llm_request_duration_seconds.labels(
        provider="mock", model="mock-1", purpose="planner"
    ).observe(0.4)
    metrics.llm_tokens_total.labels(provider="mock", model="mock-1", direction="input").inc(120)
    metrics.ws_delivery_latency_seconds.observe(0.3)

    text = exposition()
    for name in (
        "tasks_total",
        "task_duration_seconds",
        "steps_per_task",
        "verification_total",
        "recovery_attempts_total",
        "recovery_success_ratio",
        "cache_lookups_total",
        "approvals_total",
        "vision_invocations_total",
        "llm_request_duration_seconds",
        "llm_tokens_total",
        "ws_delivery_latency_seconds",
        "active_tasks",
        "queued_tasks",
        "http_request_duration_seconds",
        "http_errors_total",
    ):
        assert name in text, name


def test_no_metric_is_labelled_by_anything_unbounded() -> None:
    """A label whose values come from user input is a memory leak with a graph.

    Task ids, user ids, URLs and instructions are all per-request values. The
    HTTP metrics use the route template for exactly this reason, and this test
    is what stops a later part from adding a convenient label to one of them.
    """
    forbidden = {"task_id", "user_id", "url", "path", "instruction", "step", "step_number", "id"}

    for collector in list(metrics.REGISTRY._collector_to_names):
        names = set(getattr(collector, "_labelnames", ()))
        assert not (names & forbidden), f"{collector} is labelled by {names & forbidden}"


def test_the_recovery_ratio_counts_episodes_not_attempts() -> None:
    """Four failed steps, three rescued, whatever the rungs cost.

    Getting this wrong once reported 36% for a run that rescued 80% of what
    broke, which is the kind of metric that is worse than having none.
    """
    for rescued in (True, True, True, False):
        metrics.observe_recovery_episode(rescued=rescued)

    text = exposition()

    assert "recovery_episodes_total 4.0" in text
    assert "recovery_rescued_total 3.0" in text
    assert "recovery_success_ratio 0.75" in text


def test_the_ratio_is_zero_rather_than_undefined_before_anything_breaks() -> None:
    assert "recovery_success_ratio 0.0" in exposition()


def test_tokens_are_counted_by_direction() -> None:
    """Counts only. Nothing about a prompt or a completion is published."""
    metrics.llm_tokens_total.labels(provider="mock", model="m", direction="input").inc(100)
    metrics.llm_tokens_total.labels(provider="mock", model="m", direction="output").inc(20)

    text = exposition()

    assert 'direction="input"' in text
    assert 'direction="output"' in text


def test_the_exposition_is_prometheus_text() -> None:
    """The content type the scraper expects, and the format it parses."""
    metrics.tasks_total.labels(status="COMPLETED").inc()
    text = exposition()

    assert metrics.CONTENT_TYPE.startswith("text/plain")
    assert "# HELP tasks_total" in text
    assert "# TYPE tasks_total counter" in text


def test_reset_zeroes_an_unlabelled_counter() -> None:
    """The pollution this fixture exists to prevent.

    ``clear()`` alone empties the label map and leaves an unlabelled counter's
    own value alone, so one test's episodes turned up in the next test's total.
    """
    metrics.observe_recovery_episode(rescued=True)
    metrics.reset()

    assert "recovery_episodes_total 0.0" in exposition()
