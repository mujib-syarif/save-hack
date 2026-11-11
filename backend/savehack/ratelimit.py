"""Small in-memory sliding-window rate limiter (per client key)."""
import threading
import time
from collections import deque


class RateLimiter:
    def __init__(self, limit: int, window: float = 60.0, max_keys: int = 10_000):
        self.limit, self.window, self.max_keys = limit, window, max_keys
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits.get(key)
            if q is None:
                if len(self._hits) >= self.max_keys:
                    self._evict(now)
                q = self._hits[key] = deque()
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def retry_after(self, key: str, now: float = None) -> int:
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits.get(key)
            if not q:
                return 0
            return max(1, int(q[0] + self.window - now) + 1)

    def _evict(self, now: float) -> None:
        for k in [k for k, q in self._hits.items() if not q or q[-1] <= now - self.window]:
            self._hits.pop(k, None)
        if len(self._hits) >= self.max_keys:  # still full: drop the oldest half
            for k in list(self._hits)[: self.max_keys // 2]:
                self._hits.pop(k, None)
