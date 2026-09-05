"""
Simple in-memory sliding-window rate limiter.

Deliberately not distributed — the whole architecture (in-process
runtime registry) already requires a single Uvicorn worker for this
MVP, so an in-memory limiter is consistent with that constraint and
needs no Redis. It resets on restart, which is an acceptable trade
for a 5-10 user MVP; move to a shared store if you scale past one
process.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str, max_hits: int, window_seconds: int) -> bool:
        """Record one hit under `key`; return False if it exceeds the limit."""
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window_seconds:
                q.popleft()
            if len(q) >= max_hits:
                return False
            q.append(now)
            return True

    def sweep(self, max_age_seconds: int = 3600) -> None:
        """Optional periodic housekeeping so the dict doesn't grow forever."""
        now = time.monotonic()
        with self._lock:
            stale = [k for k, q in self._hits.items() if not q or now - q[-1] > max_age_seconds]
            for k in stale:
                self._hits.pop(k, None)


rate_limiter = InMemoryRateLimiter()
