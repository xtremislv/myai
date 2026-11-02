"""Readiness: can this gateway actually do its job right now?

/healthz says the process is alive. /readyz checks everything it depends on. It reports
each dependency by name only: error details can hold hosts or credentials.
"""
from __future__ import annotations

import logging

import httpx
from sqlalchemy import text

from app.config import get_settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 2


def _postgres() -> None:
    from app.db.session import engine

    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


def _redis() -> None:
    import redis

    redis.Redis.from_url(
        get_settings().redis_url, socket_connect_timeout=TIMEOUT_SECONDS, socket_timeout=TIMEOUT_SECONDS
    ).ping()


def _object_storage() -> None:
    from app.storage.objects import ensure_bucket

    ensure_bucket()


def _model_server() -> None:
    response = httpx.get(f"{get_settings().llm_base_url}/models", timeout=TIMEOUT_SECONDS)
    response.raise_for_status()


CHECKS = {
    "postgres": _postgres,
    "redis": _redis,
    "object_storage": _object_storage,
    "model_server": _model_server,
}


def readiness() -> tuple[bool, dict[str, str]]:
    results = {}
    for name, check in CHECKS.items():
        try:
            check()
            results[name] = "ok"
        except Exception as e:  # noqa: BLE001
            logger.warning("readiness: %s unavailable (%s)", name, type(e).__name__)
            results[name] = "unavailable"
    return all(status == "ok" for status in results.values()), results
