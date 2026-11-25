"""Unit tests for the per-domain asyncio rate limiter."""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.crawl.rate_limit import DomainRateLimiter, _default_rate_per_second


class FakeClock:
    """Monotonic clock that only advances when the fake sleep is called."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def make_limiter(
    *,
    rate_per_second: float = 1.0,
    start: float = 1000.0,
) -> tuple[DomainRateLimiter, FakeClock, list[float]]:
    clock = FakeClock(start)
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.now += seconds

    limiter = DomainRateLimiter()
    limiter.configure(rate_per_second=rate_per_second, clock=clock, sleep=_sleep)
    limiter.clear()
    return limiter, clock, sleeps


async def test_same_domain_is_paced_at_the_configured_rate() -> None:
    limiter, _, sleeps = make_limiter(rate_per_second=1.0)

    first = await limiter.acquire("tiki.vn")
    second = await limiter.acquire("tiki.vn")
    third = await limiter.acquire("tiki.vn")

    assert first == 0.0
    assert second == pytest.approx(1.0)
    assert third == pytest.approx(1.0)
    assert sleeps == pytest.approx([1.0, 1.0])


async def test_two_domains_are_independent() -> None:
    limiter, _, sleeps = make_limiter(rate_per_second=1.0)

    assert await limiter.acquire("tiki.vn") == 0.0
    assert await limiter.acquire("shopee.vn") == 0.0
    # Waiting for tiki.vn must not consume shopee.vn's slot: the shared clock
    # already advanced past it, so shopee.vn is free again immediately.
    assert await limiter.acquire("tiki.vn") == pytest.approx(1.0)
    assert await limiter.acquire("shopee.vn") == 0.0

    assert sleeps == pytest.approx([1.0])


async def test_higher_rate_shortens_the_interval() -> None:
    limiter, _, sleeps = make_limiter(rate_per_second=2.0)

    await limiter.acquire("tiki.vn")
    waited = await limiter.acquire("tiki.vn")

    assert waited == pytest.approx(0.5)
    assert sleeps == pytest.approx([0.5])


async def test_domain_keys_are_normalized() -> None:
    limiter, _, sleeps = make_limiter(rate_per_second=1.0)

    await limiter.acquire("TIKI.VN")
    waited = await limiter.acquire("  tiki.vn  ")

    assert waited == pytest.approx(1.0)
    assert len(sleeps) == 1


async def test_per_call_rate_override_wins() -> None:
    limiter, _, sleeps = make_limiter(rate_per_second=1.0)

    await limiter.acquire("tiki.vn", rate_per_second=10.0)
    waited = await limiter.acquire("tiki.vn", rate_per_second=10.0)

    assert waited == pytest.approx(0.1)


async def test_clear_resets_state() -> None:
    limiter, _, _ = make_limiter(rate_per_second=1.0)

    await limiter.acquire("tiki.vn")
    limiter.clear()

    assert await limiter.acquire("tiki.vn") == 0.0


async def test_configure_updates_rate_and_sleep() -> None:
    limiter, clock, sleeps = make_limiter(rate_per_second=1.0)
    limiter.configure(rate_per_second=4.0)

    await limiter.acquire("tiki.vn")
    waited = await limiter.acquire("tiki.vn")

    assert limiter.rate_per_second == 4.0
    assert waited == pytest.approx(0.25)
    assert sleeps == pytest.approx([0.25])
    assert clock.now == pytest.approx(1000.25)


async def test_clock_regression_does_not_reduce_the_interval() -> None:
    """If the clock moves backwards, the schedule falls back to the recorded target."""
    limiter, clock, _ = make_limiter(rate_per_second=1.0)

    await limiter.acquire("tiki.vn")
    clock.now -= 5.0  # wall clock jumped backwards
    waited = await limiter.acquire("tiki.vn")

    assert waited == pytest.approx(6.0)


def test_constructor_keeps_explicit_rate() -> None:
    from app.core.singleton import SingletonMeta

    SingletonMeta.reset(DomainRateLimiter)
    limiter = DomainRateLimiter(rate_per_second=0.5)

    assert limiter.rate_per_second == 0.5


@pytest.mark.parametrize("rate", [0, -1, -0.5])
def test_constructor_rejects_non_positive_rate(rate: float) -> None:
    from app.core.singleton import SingletonMeta

    SingletonMeta.reset(DomainRateLimiter)
    with pytest.raises(ValueError, match="rate_per_second"):
        DomainRateLimiter(rate_per_second=rate)


@pytest.mark.parametrize("rate", [0, -1, -0.5])
def test_configure_rejects_non_positive_rate(rate: float) -> None:
    limiter, _, _ = make_limiter(rate_per_second=1.0)

    with pytest.raises(ValueError, match="rate_per_second"):
        limiter.configure(rate_per_second=rate)


async def test_non_positive_per_call_rate_is_rejected() -> None:
    limiter, _, _ = make_limiter(rate_per_second=1.0)

    with pytest.raises(ValueError, match="rate_per_second"):
        await limiter.acquire("tiki.vn", rate_per_second=0)


def test_default_rate_comes_from_settings(monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("CRAWL_RATE_PER_SECOND", "3.5")
    get_settings.cache_clear()
    try:
        assert _default_rate_per_second() == 3.5
    finally:
        monkeypatch.delenv("CRAWL_RATE_PER_SECOND", raising=False)
        get_settings.cache_clear()


def test_default_rate_falls_back_when_settings_unreadable(monkeypatch) -> None:
    import app.crawl.rate_limit as module

    def _boom() -> Any:
        raise RuntimeError("no settings")

    monkeypatch.setattr(module, "get_settings", _boom)

    assert _default_rate_per_second() == 1.0


async def test_default_clock_and_sleep_are_used() -> None:
    import asyncio

    from app.core.singleton import SingletonMeta

    SingletonMeta.reset(DomainRateLimiter)
    limiter = DomainRateLimiter(rate_per_second=1000.0)

    assert limiter._clock is time.monotonic
    assert limiter._sleep is asyncio.sleep
    # First acquire for a fresh domain is always immediate.
    assert await limiter.acquire("tiki.vn") == 0.0


async def test_configure_with_none_keeps_existing_clock_and_sleep() -> None:
    limiter, clock, sleeps = make_limiter(rate_per_second=1.0)
    limiter.configure(clock=None, sleep=None)  # type: ignore[arg-type]

    await limiter.acquire("tiki.vn")
    waited = await limiter.acquire("tiki.vn")

    assert clock.now == pytest.approx(1001.0)
    assert waited == pytest.approx(1.0)
    assert sleeps == pytest.approx([1.0])
