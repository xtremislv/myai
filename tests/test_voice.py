"""Voice input (app/api/routes_voice.py): signed-in only, size and rate limits, the local Whisper service."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import routes_voice
from app.auth.security import issue_token
from app.main import app


@pytest.fixture
def client():
    routes_voice._recent.clear()
    return TestClient(app)


def _as(uid):
    return {"authorization": "Bearer " + issue_token(uid, uid, ["researcher"])}


def test_a_recording_comes_back_as_text(client, monkeypatch):
    sent = {}

    def fake(data, language=""):
        sent.update(data=data, language=language)
        return {"text": "find me rare gadgets under 2000 rupees", "language": "en", "seconds": 3.2}

    monkeypatch.setattr(routes_voice, "transcribe", fake)
    r = client.post("/v1/voice/transcribe", files={"audio": ("voice.webm", b"OggS-audio", "audio/webm")},
                    data={"language": "hi"}, headers=_as("u1"))
    assert r.status_code == 200 and r.json()["text"].startswith("find me rare gadgets")
    assert sent == {"data": b"OggS-audio", "language": "hi"}


def test_only_signed_in_people_and_sensible_sizes(client, monkeypatch):
    monkeypatch.setattr(routes_voice, "transcribe", lambda data, language="": {"text": "x", "language": "en", "seconds": 1})
    audio = {"audio": ("v.webm", b"x", "audio/webm")}
    assert client.post("/v1/voice/transcribe", files=audio).status_code == 401
    assert client.post("/v1/voice/transcribe", files={"audio": ("v.webm", b"", "audio/webm")},
                       headers=_as("u1")).status_code == 400
    monkeypatch.setattr(routes_voice, "MAX_BYTES", 10)
    assert client.post("/v1/voice/transcribe", files={"audio": ("v.webm", b"x" * 11, "audio/webm")},
                       headers=_as("u1")).status_code == 413


def test_too_many_recordings_in_a_minute_wait(client, monkeypatch):
    monkeypatch.setattr(routes_voice, "transcribe", lambda data, language="": {"text": "x", "language": "en", "seconds": 1})
    monkeypatch.setattr(routes_voice, "PER_MINUTE", 2)
    audio = {"audio": ("v.webm", b"x", "audio/webm")}
    codes = [client.post("/v1/voice/transcribe", files=audio, headers=_as("u1")).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert client.post("/v1/voice/transcribe", files=audio, headers=_as("u2")).status_code == 200  # per person


def test_a_stopped_service_says_so():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        routes_voice.transcribe(b"x", base="http://127.0.0.1:9")  # nothing listens there
    assert e.value.status_code == 503 and "isn't running" in e.value.detail
    with pytest.raises(HTTPException) as e:
        routes_voice.transcribe(b"x", base="")
    assert e.value.status_code == 503
