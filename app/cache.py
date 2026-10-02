"""A tiny thread-safe TTL cache so the iPad's polling doesn't hit Google on every request."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
from typing import TypeVar

T = TypeVar("T")


class TTLCache:
    def __init__(self) -> None:
        self._data: dict[Hashable, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get_or_set(self, key: Hashable, ttl: float, compute: Callable[[], T]) -> T:
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit and hit[0] > now:
                return hit[1]  # type: ignore[return-value]
        value = compute()
        with self._lock:
            self._data = {k: v for k, v in self._data.items() if v[0] > now}
            self._data[key] = (now + ttl, value)
        return value

    def discard(self, matches: Callable[[Hashable], bool]) -> None:
        with self._lock:
            self._data = {k: v for k, v in self._data.items() if not matches(k)}

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
