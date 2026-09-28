"""What a run costs (spec section 7).

"1 credit per agent step; +1 extra for a vision-grounded step; replayed steps
0.5, round up per task". Three sentences with four decisions hidden in them,
which is why this has its own file.
"""

import pytest

from app.core.config import Settings
from app.domain.credits import (
    CreditPrices,
    Usage,
    credits_for,
    credits_for_usage,
    reservation_for,
)


def test_a_plain_step_costs_one() -> None:
    assert credits_for(7) == 7


def test_a_vision_step_costs_two() -> None:
    """Not three: the two is the whole price, not a surcharge on top of one."""
    assert credits_for(1, vision_steps=1) == 2
    assert credits_for(7, vision_steps=3) == 4 + 6


def test_a_replayed_step_costs_half() -> None:
    assert credits_for(4, replayed_steps=4) == 2


def test_the_rounding_is_per_task_not_per_step() -> None:
    """The difference between three replayed steps costing two and costing three.

    Rounding each half up would charge a full credit for every replayed step,
    which is the same as not discounting them at all.
    """
    assert credits_for(3, replayed_steps=3) == 2


def test_a_mixed_task_adds_up() -> None:
    # Ten steps: two vision at 2, four replayed at 0.5, four plain at 1.
    assert credits_for(10, vision_steps=2, replayed_steps=4) == 4 + 2 + 4


def test_an_odd_mix_rounds_up_once() -> None:
    # 1 plain + 1 vision (2) + 3 replayed (1.5) = 4.5 -> 5.
    assert credits_for(5, vision_steps=1, replayed_steps=3) == 5


def test_the_half_price_can_be_switched_off() -> None:
    prices = CreditPrices(replay_is_half=False)

    assert credits_for(4, replayed_steps=4, prices=prices) == 4


def test_a_run_with_no_steps_costs_nothing() -> None:
    """Which is what makes the difference between a settle and a release."""
    assert credits_for(0) == 0
    assert credits_for(0, vision_steps=3) == 0


def test_counts_larger_than_the_step_total_are_clamped() -> None:
    """Defensive rather than expected.

    The three counts come from different counters in the loop, and a charge that
    exceeded the reservation because one of them drifted would be a bill nobody
    could explain.
    """
    assert credits_for(2, vision_steps=9) == 4
    assert credits_for(2, vision_steps=1, replayed_steps=9) == 3


def test_negative_counts_do_not_produce_a_credit() -> None:
    assert credits_for(3, vision_steps=-5) == 3
    assert credits_for(-1) == 0


def test_the_prices_come_from_settings(settings: Settings) -> None:
    dearer = settings.model_copy(update={"CREDITS_PER_STEP": 3, "CREDITS_PER_VISION_STEP": 10})

    prices = CreditPrices.from_settings(dearer)

    assert credits_for(2, vision_steps=1, prices=prices) == 3 + 10


def test_usage_and_the_loose_form_agree() -> None:
    """Two entry points, one answer. The loop uses one and the stats the other."""
    usage = Usage(steps=9, vision_steps=2, replayed_steps=4)

    assert credits_for_usage(usage) == credits_for(9, vision_steps=2, replayed_steps=4)


@pytest.mark.parametrize(("steps", "multiplier", "expected"), [(25, 2, 50), (10, 1, 10), (0, 2, 0)])
def test_the_reservation_is_max_steps_times_the_multiplier(
    steps: int, multiplier: int, expected: int
) -> None:
    """Spec section 7: reserve MAX_STEPS x 2.

    Deliberately not the real cost, which nobody knows yet. It is an upper bound
    generous enough that a task grounding on every step is still covered.
    """
    assert reservation_for(steps, multiplier) == expected


def test_the_reservation_covers_the_worst_case() -> None:
    """The multiplier and the vision price have to agree, or a task can outrun
    its own hold."""
    max_steps = 25
    held = reservation_for(max_steps, 2)
    worst = credits_for(max_steps, vision_steps=max_steps)

    assert held >= worst
