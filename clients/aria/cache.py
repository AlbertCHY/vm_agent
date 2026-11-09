"""行程內的簡易 TTL 快取 (catalog、form、projects、images 這類很少變動的資料)"""
import threading
import time
from typing import Any, Callable, Hashable


class TTLCache:
    def __init__(self, ttl_seconds: float = 300):
        self.ttl = ttl_seconds
        self._data: dict[Hashable, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get_or_set(self, key: Hashable, factory: Callable[[], Any]) -> Any:
        now = time.time()
        with self._lock:
            hit = self._data.get(key)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        value = factory()
        with self._lock:
            self._data[key] = (now, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
