"""Per-department rate limiting (fixed one-minute window).

Two interchangeable implementations behind a tiny protocol:
  * RedisQuota     — shared across gateway replicas (production)
  * InMemoryQuota  — process-local (tests / single process)
"""
from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class QuotaLimiter(Protocol):
    def allow(self, department: str, limit_per_min: int) -> bool:
        """Record one hit; return True if still within the per-minute limit."""
        ...


class InMemoryQuota:
    def __init__(self) -> None:
        self._win: dict[tuple[str, int], int] = {}

    def allow(self, department: str, limit_per_min: int) -> bool:
        minute = int(time.time() // 60)
        key = (department, minute)
        self._win[key] = self._win.get(key, 0) + 1
        # opportunistic cleanup of old windows
        self._win = {k: v for k, v in self._win.items() if k[1] >= minute - 1}
        return self._win[key] <= limit_per_min


class RedisQuota:
    def __init__(self, client) -> None:
        self._r = client

    def allow(self, department: str, limit_per_min: int) -> bool:
        minute = int(time.time() // 60)
        key = f"quota:{department}:{minute}"
        n = self._r.incr(key)
        if n == 1:
            self._r.expire(key, 120)
        return int(n) <= limit_per_min


def default_quota() -> QuotaLimiter:
    """Build a Redis limiter from settings; fall back to in-memory if Redis is down."""
    try:
        import redis

        from app.config import get_settings

        client = redis.Redis.from_url(get_settings().redis_url)
        client.ping()
        return RedisQuota(client)
    except Exception:
        return InMemoryQuota()
