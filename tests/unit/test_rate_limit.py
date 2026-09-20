"""B07: the token bucket."""

import asyncio

import pytest

from app.core.rate_limit import InMemoryRateLimiter, RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def limiter(clock: FakeClock) -> InMemoryRateLimiter:
    return InMemoryRateLimiter(clock=clock)


async def test_allows_up_to_the_limit(limiter: RateLimiter) -> None:
    allowed = [(await limiter.acquire("k", limit=5)).allowed for _ in range(5)]

    assert allowed == [True] * 5


async def test_blocks_past_the_limit(limiter: RateLimiter) -> None:
    for _ in range(5):
        await limiter.acquire("k", limit=5)

    result = await limiter.acquire("k", limit=5)

    assert not result.allowed
    assert result.remaining == 0


async def test_retry_after_is_whole_seconds_and_never_zero(limiter: RateLimiter) -> None:
    """Retry-After: 0 would invite an immediate retry."""
    for _ in range(5):
        await limiter.acquire("k", limit=5)

    result = await limiter.acquire("k", limit=5)

    assert result.retry_after >= 1
    assert isinstance(result.retry_after, int)


async def test_refills_over_time(limiter: RateLimiter, clock: FakeClock) -> None:
    """At 5 per minute one token returns every twelve seconds."""
    for _ in range(5):
        await limiter.acquire("k", limit=5)
    assert not (await limiter.acquire("k", limit=5)).allowed

    clock.advance(12)

    assert (await limiter.acquire("k", limit=5)).allowed


async def test_refill_is_gradual_not_a_full_reset(limiter: RateLimiter, clock: FakeClock) -> None:
    """A fixed window would hand back the whole quota at the boundary."""
    for _ in range(5):
        await limiter.acquire("k", limit=5)

    clock.advance(12)

    assert (await limiter.acquire("k", limit=5)).allowed
    assert not (await limiter.acquire("k", limit=5)).allowed


async def test_bucket_never_exceeds_capacity(limiter: RateLimiter, clock: FakeClock) -> None:
    await limiter.acquire("k", limit=5)
    clock.advance(3600)

    allowed = [(await limiter.acquire("k", limit=5)).allowed for _ in range(6)]

    assert allowed == [True] * 5 + [False]


async def test_keys_are_independent(limiter: RateLimiter) -> None:
    for _ in range(5):
        await limiter.acquire("login:1.2.3.4", limit=5)

    assert (await limiter.acquire("login:9.9.9.9", limit=5)).allowed
    assert (await limiter.acquire("register:1.2.3.4", limit=5)).allowed


async def test_reset_one_key(limiter: RateLimiter) -> None:
    for _ in range(5):
        await limiter.acquire("k", limit=5)

    await limiter.reset("k")

    assert (await limiter.acquire("k", limit=5)).allowed


async def test_reset_everything(limiter: RateLimiter) -> None:
    for _ in range(5):
        await limiter.acquire("a", limit=5)
        await limiter.acquire("b", limit=5)

    await limiter.reset()

    assert (await limiter.acquire("a", limit=5)).allowed
    assert (await limiter.acquire("b", limit=5)).allowed


async def test_zero_limit_blocks_everything(limiter: RateLimiter) -> None:
    assert not (await limiter.acquire("k", limit=0)).allowed


async def test_concurrent_callers_cannot_exceed_the_limit(limiter: RateLimiter) -> None:
    """The lock is what stops two coroutines reading the same token count."""
    results = await asyncio.gather(*(limiter.acquire("k", limit=5) for _ in range(20)))

    assert sum(1 for r in results if r.allowed) == 5


async def test_idle_buckets_are_eventually_evicted(clock: FakeClock) -> None:
    """A stream of unique addresses must not grow the dict forever."""
    limiter = InMemoryRateLimiter(clock=clock)
    for index in range(300):
        await limiter.acquire(f"ip-{index}", limit=5)

    clock.advance(3600)
    for index in range(300):
        await limiter.acquire(f"later-{index}", limit=5)

    assert len(limiter._buckets) < 600
