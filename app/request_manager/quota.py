"""Per-department rate limiting (fixed one-minute window).

Two interchangeable implementations behind a tiny protocol:
  * RedisQuota     — shared across gateway replicas (production)
  * InMemoryQuota  — process-local (tests / fallback when Redis is down)
"""
from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


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


class FailoverQuota:
    """Use the primary limiter, and count in the fallback whenever it errors, so a
    Redis outage degrades to per-process limits rather than no limits."""

    def __init__(self, primary: QuotaLimiter, fallback: QuotaLimiter) -> None:
        self._primary = primary
        self._fallback = fallback

    def allow(self, department: str, limit_per_min: int) -> bool:
        try:
            return self._primary.allow(department, limit_per_min)
        except Exception:  # noqa: BLE001
            logger.warning("quota backend unavailable; using in-process limits", exc_info=True)
            return self._fallback.allow(department, limit_per_min)


@lru_cache
def default_quota() -> QuotaLimiter:
    """The process-wide limiter. Cached: the fallback's counts must outlive a request."""
    import redis

    from app.config import get_settings

    client = redis.Redis.from_url(
        get_settings().redis_url, socket_connect_timeout=0.25, socket_timeout=0.25
    )
    return FailoverQuota(RedisQuota(client), InMemoryQuota())
