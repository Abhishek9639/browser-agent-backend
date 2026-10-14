"""B09: per-task step and time budgets (spec section 4.2)."""

import pytest

from app.agent.budget import BudgetExceeded, BudgetReason, TaskBudget
from app.core.config import Settings


class FakeClock:
    """Injected so a five minute timeout does not take five minutes to test."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# ------------------------------------------------------------------ steps


def test_steps_are_numbered_from_one(clock: FakeClock) -> None:
    budget = TaskBudget(max_steps=3, max_seconds=100, clock=clock)

    assert [budget.consume_step() for _ in range(3)] == [1, 2, 3]


def test_running_out_of_steps(clock: FakeClock) -> None:
    budget = TaskBudget(max_steps=2, max_seconds=100, clock=clock)
    budget.consume_step()
    budget.consume_step()

    with pytest.raises(BudgetExceeded) as caught:
        budget.consume_step()

    assert caught.value.reason is BudgetReason.MAX_STEPS
    assert "2 steps" in caught.value.message


def test_check_passes_while_steps_remain(clock: FakeClock) -> None:
    budget = TaskBudget(max_steps=2, max_seconds=100, clock=clock)
    budget.consume_step()

    budget.check()  # must not raise


def test_steps_remaining_counts_down(clock: FakeClock) -> None:
    budget = TaskBudget(max_steps=3, max_seconds=100, clock=clock)

    assert budget.steps_remaining == 3
    budget.consume_step()
    assert budget.steps_remaining == 2


def test_a_check_alone_does_not_spend_a_step(clock: FakeClock) -> None:
    """Recovery retries and approval pauses must not eat the allowance."""
    budget = TaskBudget(max_steps=1, max_seconds=100, clock=clock)

    for _ in range(10):
        budget.check()

    assert budget.steps_used == 0
    assert budget.consume_step() == 1


# ------------------------------------------------------------------ time


def test_running_out_of_time(clock: FakeClock) -> None:
    budget = TaskBudget(max_steps=100, max_seconds=300, clock=clock)

    clock.advance(300)

    with pytest.raises(BudgetExceeded) as caught:
        budget.check()
    assert caught.value.reason is BudgetReason.MAX_TIME


def test_time_is_fine_right_up_to_the_limit(clock: FakeClock) -> None:
    budget = TaskBudget(max_steps=100, max_seconds=300, clock=clock)

    clock.advance(299.9)

    budget.check()  # must not raise


def test_elapsed_and_remaining(clock: FakeClock) -> None:
    budget = TaskBudget(max_steps=100, max_seconds=300, clock=clock)

    clock.advance(120)

    assert budget.elapsed_seconds == 120
    assert budget.seconds_remaining == 180


def test_remaining_never_goes_negative(clock: FakeClock) -> None:
    budget = TaskBudget(max_steps=100, max_seconds=10, clock=clock)

    clock.advance(999)

    assert budget.seconds_remaining == 0.0


def test_a_hung_page_is_caught_by_time_not_steps(clock: FakeClock) -> None:
    """A page that hangs burns no steps; without the clock it loops forever."""
    budget = TaskBudget(max_steps=25, max_seconds=300, clock=clock)
    budget.consume_step()

    clock.advance(301)

    with pytest.raises(BudgetExceeded) as caught:
        budget.check()
    assert caught.value.reason is BudgetReason.MAX_TIME
    assert budget.steps_remaining == 24


def test_time_is_reported_before_steps(clock: FakeClock) -> None:
    """Both exhausted means the clock stopped it; naming steps misleads."""
    budget = TaskBudget(max_steps=1, max_seconds=10, clock=clock)
    budget.consume_step()

    clock.advance(11)

    with pytest.raises(BudgetExceeded) as caught:
        budget.check()
    assert caught.value.reason is BudgetReason.MAX_TIME


# ------------------------------------------------------------------ misc


def test_exhausted_is_the_non_raising_form(clock: FakeClock) -> None:
    budget = TaskBudget(max_steps=1, max_seconds=100, clock=clock)

    assert not budget.exhausted
    budget.consume_step()
    assert budget.exhausted


def test_reset_restores_both_allowances(clock: FakeClock) -> None:
    """A replay that falls back to planning starts its budget again."""
    budget = TaskBudget(max_steps=2, max_seconds=100, clock=clock)
    budget.consume_step()
    clock.advance(90)

    budget.reset()

    assert budget.steps_used == 0
    assert budget.elapsed_seconds == 0


def test_from_settings_uses_the_spec_defaults() -> None:
    budget = TaskBudget.from_settings(Settings())

    assert budget.max_steps == 25
    assert budget.max_seconds == 300


@pytest.mark.parametrize(("steps", "seconds"), [(0, 100), (-1, 100), (10, 0), (10, -5)])
def test_nonsense_limits_are_refused(steps: int, seconds: float) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        TaskBudget(max_steps=steps, max_seconds=seconds)


def test_the_clock_is_monotonic_by_default() -> None:
    """A wall-clock adjustment must not hand out extra seconds."""
    import time

    budget = TaskBudget(max_steps=1, max_seconds=1)

    assert budget._clock is time.monotonic


def test_time_spent_waiting_on_a_person_does_not_count() -> None:
    now = [0.0]
    budget = TaskBudget(max_steps=10, max_seconds=100.0, clock=lambda: now[0])

    now[0] = 90.0
    budget.exclude(80.0)  # eighty seconds were spent waiting for a login

    assert budget.elapsed_seconds == 10.0
    budget.check()  # still inside the limit
