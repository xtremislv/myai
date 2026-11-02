"""Limits how many runs may hold the GPU at once.

Quotas limit requests per minute. This limits requests in progress, so a few slow ones
from one department or person can't queue up everyone else on a single GPU. A request
over the limit is turned away straight away instead of waiting.

Same pattern as quota.py: Redis shared across replicas, with per-process counting when
Redis is down, so an outage degrades to weaker limits rather than none.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Protocol

logger = logging.getLogger(__name__)

# A crashed process can't release its slots, so counts expire on their own.
SLOT_TTL_SECONDS = 900


class SlotBackend(Protocol):
    def acquire(self, key: str, limit: int) -> bool: ...

    def release(self, key: str) -> None: ...


class InMemorySlots:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: dict[str, int] = {}

    def acquire(self, key: str, limit: int) -> bool:
        with self._lock:
            if self._held.get(key, 0) >= limit:
                return False
            self._held[key] = self._held.get(key, 0) + 1
            return True

    def release(self, key: str) -> None:
        with self._lock:
            remaining = self._held.get(key, 0) - 1
            if remaining > 0:
                self._held[key] = remaining
            else:
                self._held.pop(key, None)


class RedisSlots:
    def __init__(self, client) -> None:
        self._r = client

    def acquire(self, key: str, limit: int) -> bool:
        name = f"slots:{key}"
        held = self._r.incr(name)
        self._r.expire(name, SLOT_TTL_SECONDS)
        if int(held) > limit:
            self._r.decr(name)
            return False
        return True

    def release(self, key: str) -> None:
        name = f"slots:{key}"
        if int(self._r.decr(name)) <= 0:
            self._r.delete(name)


class ConcurrencyLimiter:
    def __init__(self, primary: SlotBackend, fallback: SlotBackend | None = None) -> None:
        self._primary = primary
        self._fallback = fallback or primary

    @contextmanager
    def slot(self, key: str, limit: int) -> Iterator[bool]:
        """Yields whether a slot was free. It is given back on the way out, to the same
        backend that gave it, even if Redis went down in between."""
        backend = self._primary
        try:
            free = backend.acquire(key, limit)
        except Exception:  # noqa: BLE001
            logger.warning("concurrency backend unavailable; using in-process limits")
            backend = self._fallback
            free = backend.acquire(key, limit)
        try:
            yield free
        finally:
            if free:
                try:
                    backend.release(key)
                except Exception:  # noqa: BLE001 - the count expires on its own
                    logger.warning("could not release a concurrency slot")


@lru_cache
def default_concurrency() -> ConcurrencyLimiter:
    import redis

    from app.config import get_settings

    client = redis.Redis.from_url(
        get_settings().redis_url, socket_connect_timeout=0.25, socket_timeout=0.25
    )
    return ConcurrencyLimiter(RedisSlots(client), InMemorySlots())
