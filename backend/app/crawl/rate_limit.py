"""Per-domain asyncio-safe rate limiter (Singleton)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from app.core.config import get_settings
from app.core.singleton import SingletonMeta

ClockFn = Callable[[], float]
SleepFn = Callable[[float], Awaitable[None]]


def _default_rate_per_second() -> float:
    try:
        rate = float(get_settings().CRAWL_RATE_PER_SECOND)
        return rate if rate > 0 else 1.0
    except Exception:
        return 1.0


class DomainRateLimiter(metaclass=SingletonMeta):
    """Singleton rate limiter enforcing a minimum per-domain request interval.

    Each domain owns an independent ``asyncio.Lock`` and next-allowed timestamp,
    so requests to different domains do not block one another.
    """

    def __init__(
        self,
        *,
        rate_per_second: float | None = None,
        clock: ClockFn | None = None,
        sleep: SleepFn | None = None,
    ) -> None:
        self._rate_per_second: float = (
            rate_per_second if rate_per_second is not None else _default_rate_per_second()
        )
        if self._rate_per_second <= 0:
            raise ValueError("rate_per_second must be > 0.")
        self._clock: ClockFn = clock if clock is not None else time.monotonic
        self._sleep: SleepFn = sleep if sleep is not None else asyncio.sleep
        self._locks: dict[str, asyncio.Lock] = {}
        self._next_allowed: dict[str, float] = {}

    @property
    def rate_per_second(self) -> float:
        return self._rate_per_second

    def configure(
        self,
        *,
        rate_per_second: float | None = None,
        clock: ClockFn | None = None,
        sleep: SleepFn | None = None,
    ) -> None:
        """Reconfigure rate, clock, or sleep coroutine (useful in tests)."""
        if rate_per_second is not None:
            if rate_per_second <= 0:
                raise ValueError("rate_per_second must be > 0.")
            self._rate_per_second = rate_per_second
        if clock is not None:
            self._clock = clock
        if sleep is not None:
            self._sleep = sleep

    def clear(self) -> None:
        """Reset all per-domain scheduling state."""
        self._locks.clear()
        self._next_allowed.clear()

    def _get_lock(self, domain: str) -> asyncio.Lock:
        lock = self._locks.get(domain)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[domain] = lock
        return lock

    async def acquire(self, domain: str, *, rate_per_second: float | None = None) -> float:
        """Wait until the next request slot for ``domain`` is available.

        Returns the number of seconds slept (``0.0`` if immediately allowed).
        """
        rate = rate_per_second if rate_per_second is not None else self._rate_per_second
        if rate <= 0:
            raise ValueError("rate_per_second must be > 0.")
        interval = 1.0 / rate
        key = domain.strip().lower()

        lock = self._get_lock(key)
        async with lock:
            now = self._clock()
            target = self._next_allowed.get(key)
            waited = 0.0
            if target is not None and target > now:
                waited = target - now
                await self._sleep(waited)
                after = self._clock()
                base = after if after >= target else target
            else:
                base = now
            self._next_allowed[key] = base + interval
            return waited
