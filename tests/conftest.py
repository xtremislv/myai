from __future__ import annotations

import base64
import os

# Stable test secrets. Set before any app import, because settings are read at import time.
os.environ.setdefault("JWT_SECRET", "test-only-secret-" + "x" * 32)
os.environ.setdefault("VAULT_MASTER_KEY", base64.b64encode(b"t" * 32).decode())

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app import config  # noqa: E402
from app.acl.models import Subject  # noqa: E402
from app.api import deps  # noqa: E402
from app.request_manager import concurrency, guards  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_settings():
    """Some fixtures override secrets and leave the settings cache stale."""
    config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def local_concurrency_limits(monkeypatch):
    """No Redis in tests."""
    limiter = concurrency.ConcurrencyLimiter(concurrency.InMemorySlots())
    monkeypatch.setattr(concurrency, "default_concurrency", lambda: limiter)


_real_subject_from_claims = deps.subject_from_claims


@pytest.fixture(autouse=True)
def tokens_without_a_user_row(monkeypatch):
    """Most tests issue tokens directly instead of creating accounts, so a token with no
    user row falls back to its own roles. test_live_roles.py tests the real behaviour."""

    def _lookup(session, claims):
        try:
            return _real_subject_from_claims(session, claims)
        except HTTPException:
            return Subject(id=claims["sub"], roles=tuple(claims.get("roles", [])))

    monkeypatch.setattr(deps, "subject_from_claims", _lookup)


@pytest.fixture(autouse=True)
def nsfw_model_unavailable(monkeypatch):
    """Tests script the LLM's replies, so the classifiers must not consume them. Guards
    use their keyword fallback unless a test sets a verdict."""
    monkeypatch.setattr(guards, "model_verdict", lambda message: None)
    monkeypatch.setattr(guards, "forward_verdict", lambda message: None)
    monkeypatch.setattr(guards, "triage_verdict", lambda message, departments, recent, others=None: None)
