"""Voice input: a recording from the chat box (web or Android) comes back as text for the person to check
and send. Transcription runs on this machine (scripts/stt_server.py, Whisper large-v3-turbo); the audio
and the text are never stored or logged. The text goes into the chat box, not straight to the agents, so
it passes the usual triage and guards when the person sends it."""
from __future__ import annotations

import time
from collections import defaultdict, deque

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.acl.models import Subject
from app.api.deps import current_subject
from app.config import get_settings

router = APIRouter(prefix="/v1/voice", tags=["voice"])
MAX_BYTES = 12 * 1024 * 1024  # ~3 minutes of browser Opus or phone AAC, with room to spare
PER_MINUTE = 20
_recent: dict[str, deque[float]] = defaultdict(deque)


class TranscriptOut(BaseModel):
    text: str
    language: str
    seconds: float


def _throttle(user: str) -> None:
    now = time.monotonic()
    q = _recent[user]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= PER_MINUTE:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many recordings at once; wait a moment")
    q.append(now)


def transcribe(data: bytes, language: str = "", base: str | None = None) -> dict:
    base = (base if base is not None else get_settings().stt_url).rstrip("/")
    if not base:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "voice input is off on this server")
    try:
        # Local service: never through OUTBOUND_PROXY (trust_env=False ignores proxy variables).
        with httpx.Client(timeout=120, trust_env=False) as client:
            r = client.post(f"{base}/transcribe", params={"language": language}, content=data,
                            headers={"content-type": "application/octet-stream"})
    except httpx.HTTPError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "voice input isn't running right now") from None
    if r.status_code in (413, 422):
        raise HTTPException(r.status_code, r.json().get("detail", "that recording couldn't be read"))
    if r.status_code != 200:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "voice input isn't working right now")
    return r.json()


@router.post("/transcribe", response_model=TranscriptOut)
def transcribe_route(audio: UploadFile, language: str = Form(default="", max_length=8),
                     subject: Subject = Depends(current_subject)) -> TranscriptOut:
    data = audio.file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "recordings are limited to about 3 minutes")
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the recording is empty")
    _throttle(subject.id)
    out = transcribe(data, language)
    return TranscriptOut(text=str(out.get("text", "")), language=str(out.get("language", "")),
                         seconds=float(out.get("seconds", 0)))
