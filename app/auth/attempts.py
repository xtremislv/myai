"""Limits on wrong passwords and recovery codes (security audit 2026-11-18: nothing stopped guessing).

Failures are counted in a 15-minute window, in Redis (shared by every gateway process) with an
in-process fallback while Redis is down, three ways:

* the account from one address: 5. Guessing a password from one place stops quickly;
* the account from everywhere: 20. A real person can't be locked out by a few bad tries from a
  stranger, and guessing from many addresses is still capped at ~80 an hour;
* the address, over all accounts: 30. One client can't sweep usernames.

Only failures count. Success clears the account-from-this-address count. A refusal is 429 with
Retry-After and says nothing about whether the account exists. The address is the one uvicorn
sets (Cloudflare's, passed on by cloudflared on 127.0.0.1): never a header the client chose.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from functools import lru_cache

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 15 * 60
PER_ACCOUNT_AND_ADDRESS = 5
PER_ACCOUNT = 20
PER_ADDRESS = 30
REFUSAL = "Too many wrong attempts. Wait 15 minutes and try again."


class InMemoryCounts:
    def __init__(self) -> None:
        self._counts: dict[str, tuple[int, float]] = {}

    def get(self, key: str) -> int:
        n, start = self._counts.get(key, (0, 0.0))
        return n if time.time() - start < WINDOW_SECONDS else 0

    def add(self, key: str) -> int:
        now = time.time()
        self._counts = {k: v for k, v in self._counts.items() if now - v[1] < WINDOW_SECONDS}
        n, start = self._counts.get(key, (0, now))
        self._counts[key] = (n + 1, start)
        return n + 1

    def clear(self, key: str) -> None:
        self._counts.pop(key, None)


class RedisCounts:
    def __init__(self, client) -> None:
        self._r = client

    def get(self, key: str) -> int:
        return int(self._r.get(key) or 0)

    def add(self, key: str) -> int:
        n = int(self._r.incr(key))
        if n == 1:
            self._r.expire(key, WINDOW_SECONDS)
        return n

    def clear(self, key: str) -> None:
        self._r.delete(key)


class FailoverCounts:
    """Redis, and the in-process counts whenever Redis errors: an outage weakens the limit to
    per-process, never removes it."""

    def __init__(self, primary, fallback: InMemoryCounts) -> None:
        self._primary, self._fallback = primary, fallback

    def _call(self, name: str, key: str):
        try:
            return getattr(self._primary, name)(key)
        except Exception:  # noqa: BLE001
            logger.warning("attempt counts: Redis unavailable, counting in this process")
            return getattr(self._fallback, name)(key)

    def get(self, key: str) -> int:
        return self._call("get", key)

    def add(self, key: str) -> int:
        return self._call("add", key)

    def clear(self, key: str) -> None:
        self._call("clear", key)


@lru_cache
def counts() -> FailoverCounts:
    import redis

    from app.config import get_settings

    client = redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=0.25, socket_timeout=0.25)
    return FailoverCounts(RedisCounts(client), InMemoryCounts())


def address(request: Request | None) -> str:
    return (request.client.host if request is not None and request.client else "") or "unknown"


def _keys(kind: str, account: str, where: str) -> list[tuple[str, int]]:
    account = account.strip().lower()[:128]
    return [
        (f"attempts:{kind}:acct-addr:{account}:{where}", PER_ACCOUNT_AND_ADDRESS),
        (f"attempts:{kind}:acct:{account}", PER_ACCOUNT),
        (f"attempts:addr:{where}", PER_ADDRESS),
    ]


class Guard:
    """One check of a password or code: `guard.check()` before, then `guard.failed()` or
    `guard.succeeded()`. kind: "login" (also phones and recovery) or "reauth" (a signed-in
    person typing their password again: counted by user id)."""

    def __init__(self, kind: str, account: str, request: Request | None) -> None:
        self.keys = _keys(kind, account, address(request))
        self.store = counts()

    def check(self) -> None:
        if any(self.store.get(key) >= limit for key, limit in self.keys):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, REFUSAL,
                                headers={"Retry-After": str(WINDOW_SECONDS)})

    def failed(self) -> None:
        for key, _ in self.keys:
            self.store.add(key)

    def succeeded(self) -> None:
        self.store.clear(self.keys[0][0])



@contextmanager
def guarded(kind: str, account: str, request: Request | None, wrong: tuple[int, ...] = (401,)):
    """Check the limits, run the block, and count it as a failure if it ends in one of the
    `wrong` statuses (a wrong password or code), or clear the count if it succeeds."""
    guard = Guard(kind, account, request)
    guard.check()
    try:
        yield guard
    except HTTPException as e:
        if e.status_code in wrong:
            guard.failed()
        raise
    guard.succeeded()
