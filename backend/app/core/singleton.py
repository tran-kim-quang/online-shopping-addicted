"""Thread-safe Singleton metaclass with test reset support."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


class _ResetDescriptor:
    """Allows calling reset() both on SingletonMeta and on individual singleton classes."""

    def __get__(self, obj: type | None, owner: type) -> Callable[..., None]:
        if obj is None:

            def _reset_meta(cls: type | None = None) -> None:
                with SingletonMeta._lock:
                    if cls is None:
                        SingletonMeta._instances.clear()
                    else:
                        SingletonMeta._instances.pop(cls, None)

            return _reset_meta

        def _reset_cls(cls: type | None = None) -> None:
            target = obj if cls is None else cls
            with SingletonMeta._lock:
                SingletonMeta._instances.pop(target, None)

        return _reset_cls


class SingletonMeta(type):
    """Thread-safe singleton metaclass using double-checked locking.

    Used for shared, expensive process-wide resources (Database, RedisClient,
    SecretBox, LLMClientRegistry). Supports ``SingletonMeta.reset()`` or
    ``SingletonMeta.reset(Cls)`` (as well as ``Cls.reset()``) to clear cached
    instances between tests.
    """

    _instances: dict[type, Any] = {}
    _lock: threading.Lock = threading.Lock()
    reset = _ResetDescriptor()

    def __call__(cls, *args: Any, **kwargs: Any) -> Any:
        if cls not in SingletonMeta._instances:
            with SingletonMeta._lock:
                if cls not in SingletonMeta._instances:
                    instance = super().__call__(*args, **kwargs)
                    SingletonMeta._instances[cls] = instance
        return SingletonMeta._instances[cls]
