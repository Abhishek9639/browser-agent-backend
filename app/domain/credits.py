"""What a run costs, in credits (spec section 7).

One function, in the domain layer, because three places need the same answer
and they must not drift: the orchestrator reports it on the run, the stats
endpoint totals it over history, and the outbox settles it with the payments
service. A settle that disagrees with what the agent reported is the one bug
here that costs somebody money.

The prices are settings rather than constants. The spec calls its own figures a
placeholder, and the payments service holds the same numbers: keeping them
configurable at least makes a disagreement a deployment mistake rather than a
code change in two repositories.
"""

import math
from dataclasses import dataclass
from typing import Final

from app.core.config import Settings

__all__ = [
    "CreditPrices",
    "Usage",
    "credits_for",
    "credits_for_usage",
    "reservation_for",
]

# Spec section 7: "1 credit per agent step; +1 extra for a vision-grounded
# step; replayed steps 0.5, round up per task".
DEFAULT_PER_STEP: Final = 1
DEFAULT_PER_VISION_STEP: Final = 2


@dataclass(frozen=True, slots=True)
class Usage:
    """What a finished run consumed, before it becomes a number of credits.

    The three counts are disjoint: a replayed step never grounds, because a
    cached step names its target with a locator and has nothing for vision to
    correct.
    """

    steps: int
    vision_steps: int = 0
    replayed_steps: int = 0


@dataclass(frozen=True, slots=True)
class CreditPrices:
    """The three numbers a charge is built from."""

    per_step: int = DEFAULT_PER_STEP
    per_vision_step: int = DEFAULT_PER_VISION_STEP
    replay_is_half: bool = True

    @classmethod
    def from_settings(cls, settings: Settings) -> "CreditPrices":
        """The configured prices.

        Here rather than at each call site so the orchestrator, the stats
        endpoint and the outbox cannot end up reading different fields.
        """
        return cls(
            per_step=settings.CREDITS_PER_STEP,
            per_vision_step=settings.CREDITS_PER_VISION_STEP,
            replay_is_half=settings.CREDITS_REPLAY_STEP_HALF,
        )


def credits_for(
    steps: int,
    *,
    vision_steps: int = 0,
    replayed_steps: int = 0,
    prices: CreditPrices | None = None,
) -> int:
    """What a finished run is charged.

    The three kinds of step are disjoint by construction. A replayed step never
    grounds, because a cached step names its target with a locator and has
    nothing for vision to correct, so no step is both replayed and vision.

    Rounded up once, over the whole task, which is what the spec says and is
    also the only reading that makes two replayed steps cost one credit rather
    than two. Rounding per step would charge a full credit for each half.
    """
    prices = prices or CreditPrices()
    counted_vision = max(0, min(vision_steps, steps))
    counted_replay = max(0, min(replayed_steps, steps - counted_vision))
    plain = max(0, steps - counted_vision - counted_replay)

    replay_price = prices.per_step / 2 if prices.replay_is_half else float(prices.per_step)
    total = (
        plain * prices.per_step
        + counted_vision * prices.per_vision_step
        + counted_replay * replay_price
    )
    return math.ceil(total)


def reservation_for(max_steps: int, multiplier: int) -> int:
    """What to hold before a task starts (spec section 7: ``MAX_STEPS x 2``).

    Deliberately not the real cost, which nobody knows yet. It is an upper
    bound generous enough that a task using vision on every step is still
    covered by its own reservation, and it is settled down to the truth when
    the task ends.
    """
    return max(0, max_steps) * max(1, multiplier)


def credits_for_usage(usage: Usage, prices: CreditPrices | None = None) -> int:
    """:func:`credits_for`, taking the counts as one value."""
    return credits_for(
        usage.steps,
        vision_steps=usage.vision_steps,
        replayed_steps=usage.replayed_steps,
        prices=prices,
    )
