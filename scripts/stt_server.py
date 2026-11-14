"""Speech to text for the chat box (web and Android): Whisper large-v3-turbo via faster-whisper.

A separate local process (127.0.0.1:8011) so the gateway never loads GPU libraries and the model is
loaded once. Only the gateway calls it (`POST /v1/voice/transcribe` in app/api/routes_voice.py, which
checks the person is signed in); it binds to localhost and takes no auth itself. Audio arrives in any
format ffmpeg reads (WebM/Opus from browsers, AAC/MP4 from Android). It is decoded from a temporary file
in RAM (/dev/shm: MP4 can't be read from a pipe) that is deleted at once; the audio and the text are never
stored or logged.

    LD_LIBRARY_PATH=<cuBLAS 12 + cuDNN 8 dirs> .venv/bin/python -m scripts.stt_server

Settings (env): STT_MODEL (path or faster-whisper model name), STT_DEVICE (cuda|cpu|auto),
STT_PORT (8011). On this server the model takes ~1.6 GB of GPU memory next to vLLM; if the GPU
can't be used it falls back to the CPU (int8), slower but working.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
import time

import numpy as np
from fastapi import FastAPI, HTTPException, Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s stt %(message)s")
log = logging.getLogger("stt")

MODEL = os.environ.get("STT_MODEL", "/root/models/whisper-large-v3-turbo")
DEVICE = os.environ.get("STT_DEVICE", "auto")
RATE = 16000
MAX_SECONDS = 180
MAX_BYTES = 12 * 1024 * 1024
# Spellings the model can't guess from sound alone; also nudges it to keep English words in Latin script.
HINT = "TechWiser, GadgetWiser, TW-ai, gadget, Instagram, YouTube, Shorts, Reels, Amazon, Flipkart."
LANGUAGES = {"", "en", "hi", "ur", "pa", "bn", "ta", "te", "mr", "gu", "kn", "ml"}

app = FastAPI(title="tw-ai stt", docs_url=None, redoc_url=None, openapi_url=None)
_model = None
_lock = threading.Lock()  # one transcription at a time on the GPU; others wait their turn


def load():
    global _model
    from faster_whisper import WhisperModel

    if DEVICE in ("cuda", "auto"):
        try:
            _model = WhisperModel(MODEL, device="cuda", compute_type="float16")
            log.info("model loaded on the GPU")
            return
        except Exception as e:  # noqa: BLE001
            if DEVICE == "cuda":
                raise
            log.warning("GPU not usable (%s); using the CPU", type(e).__name__)
    _model = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=min(os.cpu_count() or 4, 16))
    log.info("model loaded on the CPU")


def decode(data: bytes) -> np.ndarray:
    """Any audio container -> 16 kHz mono float32."""
    shm = "/dev/shm" if os.path.isdir("/dev/shm") else None
    with tempfile.NamedTemporaryFile(dir=shm, prefix="stt-") as f:  # deleted when closed
        f.write(data)
        f.flush()
        try:
            out = subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", f.name, "-t", str(MAX_SECONDS),
                 "-f", "s16le", "-ac", "1", "-ar", str(RATE), "pipe:1"],
                capture_output=True, timeout=60, check=True).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise HTTPException(422, "that recording couldn't be read") from None
    return np.frombuffer(out, np.int16).astype(np.float32) / 32768.0


@app.on_event("startup")
def _startup() -> None:
    load()


@app.get("/health")
def health() -> dict:
    return {"ok": _model is not None}


@app.post("/transcribe")
async def transcribe(request: Request, language: str = "") -> dict:
    data = await request.body()
    if not data:
        raise HTTPException(422, "no audio")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "recording too long")
    language = language.strip().lower()
    if language not in LANGUAGES:
        raise HTTPException(422, "unsupported language")
    audio = decode(data)
    seconds = len(audio) / RATE
    if seconds < 0.3:
        return {"text": "", "language": language or "", "seconds": round(seconds, 1)}
    started = time.monotonic()
    with _lock:
        segments, info = _model.transcribe(
            audio, language=language or None, beam_size=5, vad_filter=True, initial_prompt=HINT,
            condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segments).strip()
    # Never log the text: it is what the person said.
    log.info("transcribed %.1fs of audio in %.2fs (%s)", seconds, time.monotonic() - started, info.language)
    return {"text": text, "language": info.language, "seconds": round(seconds, 1)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STT_PORT", "8011")), log_level="warning")
