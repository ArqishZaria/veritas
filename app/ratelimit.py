"""Single-instance sliding-window rate limiter (in-process, per key)."""
import math
import threading
import time
from collections import defaultdict, deque

from app.core.db import get_db


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: float):
        self.limit, self.window = limit, window_seconds
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str):
        """Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False, max(1, math.ceil(q[0] + self.window - now))
            q.append(now)
            return True, 0


class DbSlidingWindowLimiter:
    """Same contract as SlidingWindowLimiter, but the hits are rows in the shared database,
    so every replica behind the load balancer sees (and enforces) the same counters (A3)."""

    def __init__(self, limit: int, window_seconds: float):
        self.limit, self.window = limit, window_seconds

    def check(self, key: str):
        """Returns (allowed, retry_after_seconds). Wall clock: the clock is shared by all replicas."""
        now = time.time()
        with get_db(write=True) as conn:  # BEGIN IMMEDIATE: check+insert is atomic across processes
            conn.execute("DELETE FROM rate_limit_hits WHERE ts <= ?", (now - self.window,))
            n, oldest = conn.execute("SELECT COUNT(*), MIN(ts) FROM rate_limit_hits WHERE key = ?", (key,)).fetchone()
            if n >= self.limit:
                return False, max(1, math.ceil(oldest + self.window - now))
            conn.execute("INSERT INTO rate_limit_hits (key, ts) VALUES (?, ?)", (key, now))
            return True, 0
