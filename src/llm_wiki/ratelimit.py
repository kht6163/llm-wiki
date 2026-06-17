"""Process-local sliding-window rate limiter, shared by the web login form and the
MCP Bearer-auth gate. Kept dependency-free so any surface can import it without
pulling in FastAPI/Starlette."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    """In-memory sliding-window limiter (single process). Tracks failures per key
    and blocks once a key exceeds ``max_attempts`` within ``window_s`` seconds. A
    successful attempt should ``reset`` the key."""

    def __init__(self, max_attempts: int = 8, window_s: float = 300.0):
        self.max_attempts = max_attempts
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, dq: deque[float], now: float) -> None:
        while dq and now - dq[0] > self.window_s:
            dq.popleft()

    def allowed(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            dq = self._hits[key]
            self._prune(dq, now)
            return len(dq) < self.max_attempts

    def record_failure(self, key: str) -> bool:
        """Record one failure. Returns True only on the failure that *first* reaches
        ``max_attempts`` within the window — the just-crossed-the-threshold edge — so a
        caller can act once per window (e.g. write a single audit row) instead of on
        every failed attempt, which would let a brute-force become a write-amplification."""
        now = time.monotonic()
        with self._lock:
            dq = self._hits[key]
            self._prune(dq, now)
            dq.append(now)
            return len(dq) == self.max_attempts

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
