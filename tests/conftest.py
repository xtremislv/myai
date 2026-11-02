from __future__ import annotations

import pytest

from app.request_manager import guards


@pytest.fixture(autouse=True)
def nsfw_model_unavailable(monkeypatch):
    """Tests script the LLM's replies, so the NSFW model must not consume them.
    is_nsfw uses its keyword fallback unless a test sets a verdict."""
    monkeypatch.setattr(guards, "model_verdict", lambda message: None)
