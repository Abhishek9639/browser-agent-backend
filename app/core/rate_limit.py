"""Rate limiting.

Spec section 10 requires throttling on auth and task creation. This is a small
interface with an in-memory token bucket behind it, rather than a library, so
the algorithm is inspectable and a Redis-backed implementation can replace it
later without touching a single call site.

**In-memory means per process.** Two workers each allow the full quota, so the
effective limit is `limit x workers`. That is fine for the OJT deployment, which
runs one process. It is not fine behind a load balancer, and the fix is a shared
:class:`RateLimiter` implementation, not a bigger number here.
"""

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Final

__all__ = [
    "InMemoryRateLimiter",
    "RateLimitResult",
    "RateLimiter",
]

# Buckets idle for longer than this are dropped, so a stream of unique client
# IPs cannot grow the dict without bound.
_IDLE_EVICTION_SECONDS: Final = 900
_PRUNE_EVERY: Final = 256


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after: int
    """Whole seconds until one token is available. Zero when allowed."""


class RateLimiter(ABC):
    """Take one token from ``key``'s bucket, or report how long to wait."""

    @abstractmethod
    async def acquire(self, key: str, *, limit: int, window_seconds: int = 60) -> RateLimitResult:
        """Consume a token.

        ``limit`` is both the burst capacity and the refill rate per window, so
        ``limit=5, window_seconds=60`` means five immediately, then one more
        every twelve seconds.
        """

    @abstractmethod
    async def reset(self, key: str | None = None) -> None:
        """Forget one bucket, or all of them."""


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class InMemoryRateLimiter(RateLimiter):
    """Token bucket held in a dict.

    A token bucket rather than a fixed window because a fixed window lets a
    caller spend the whole quota in the last second of one window and again in
    the first second of the next, which is twice the intended rate at the
    boundary. The bucket refills continuously, so the average is what was asked
    for and a genuine burst is still allowed.
    """

    def __init__(self, *, clock: object = None) -> None:
        # Monotonic: a clock adjustment must not hand out free tokens.
        self._clock = clock or time.monotonic
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()
        self._since_prune = 0

    def _now(self) -> float:
        return float(self._clock())  # type: ignore[operator]

    async def acquire(self, key: str, *, limit: int, window_seconds: int = 60) -> RateLimitResult:
        if limit <= 0:
            return RateLimitResult(allowed=False, remaining=0, retry_after=window_seconds)

        refill_per_second = limit / window_seconds
        now = self._now()

        async with self._lock:
            self._maybe_prune(now)

            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(limit), updated_at=now)
                self._buckets[key] = bucket
            else:
                elapsed = max(now - bucket.updated_at, 0.0)
                bucket.tokens = min(float(limit), bucket.tokens + elapsed * refill_per_second)
                bucket.updated_at = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return RateLimitResult(
                    allowed=True,
                    remaining=int(bucket.tokens),
                    retry_after=0,
                )

            deficit = 1.0 - bucket.tokens
            # Always at least a second: Retry-After 0 invites an instant retry.
            retry_after = max(1, int(deficit / refill_per_second + 0.999))
            return RateLimitResult(allowed=False, remaining=0, retry_after=retry_after)

    async def reset(self, key: str | None = None) -> None:
        async with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)

    def _maybe_prune(self, now: float) -> None:
        """Drop idle buckets occasionally rather than on every call."""
        self._since_prune += 1
        if self._since_prune < _PRUNE_EVERY:
            return
        self._since_prune = 0
        cutoff = now - _IDLE_EVICTION_SECONDS
        stale = [key for key, bucket in self._buckets.items() if bucket.updated_at < cutoff]
        for key in stale:
            del self._buckets[key]
