from __future__ import annotations

import base64
import os

# Stable test secrets. Set before any app import, because settings are read at import time.
os.environ.setdefault("JWT_SECRET", "test-only-secret-" + "x" * 32)
os.environ.setdefault("VAULT_MASTER_KEY", base64.b64encode(b"t" * 32).decode())

import pytest  # noqa: E402

from app import config  # noqa: E402
from app.request_manager import guards  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_settings():
    """Some fixtures override secrets and leave the settings cache stale."""
    config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def nsfw_model_unavailable(monkeypatch):
    """Tests script the LLM's replies, so the classifiers must not consume them. Guards
    use their keyword fallback unless a test sets a verdict."""
    monkeypatch.setattr(guards, "model_verdict", lambda message: None)
    monkeypatch.setattr(guards, "forward_verdict", lambda message: None)
    monkeypatch.setattr(guards, "triage_verdict", lambda message, departments, recent: None)
