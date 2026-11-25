"""Unit tests for ``app.core.singleton.SingletonMeta``."""

from __future__ import annotations

import threading
import time

from app.core.singleton import SingletonMeta


class _Service(metaclass=SingletonMeta):
    def __init__(self, value: int = 1) -> None:
        self.value = value


class _OtherService(metaclass=SingletonMeta):
    pass


class _SlowService(metaclass=SingletonMeta):
    def __init__(self) -> None:
        time.sleep(0.01)
        self.ident = id(self)


def test_same_class_returns_same_instance() -> None:
    assert _Service() is _Service()
    assert _Service() is _Service(99)  # __init__ is not re-run


def test_distinct_classes_have_distinct_instances() -> None:
    assert _Service() is not _OtherService()
    assert isinstance(_Service(), _Service)
    assert isinstance(_OtherService(), _OtherService)


def test_thread_safety_single_instance() -> None:
    thread_count = 24
    barrier = threading.Barrier(thread_count)
    results: list[int] = []
    results_lock = threading.Lock()

    def _worker() -> None:
        barrier.wait()
        instance = _SlowService()
        with results_lock:
            results.append(id(instance))

    threads = [threading.Thread(target=_worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == thread_count
    assert len(set(results)) == 1


def test_reset_single_class() -> None:
    first = _Service()
    _Service.reset()

    assert _Service() is not first


def test_reset_via_metaclass_with_class_argument() -> None:
    first = _Service()
    SingletonMeta.reset(_Service)

    assert _Service() is not first


def test_reset_via_metaclass_clears_all() -> None:
    service = _Service()
    other = _OtherService()
    SingletonMeta.reset()

    assert _Service() is not service
    assert _OtherService() is not other


def test_reset_one_class_keeps_others() -> None:
    other = _OtherService()
    _Service.reset()

    assert _OtherService() is other
