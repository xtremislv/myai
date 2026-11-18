"""Videos and pictures sent to the WhatsApp bot, made into what the model can look at.

A video becomes up to MAX_FRAMES still frames spread over its length (JPEG, at most FRAME_WIDTH wide) and the
words spoken in it (local Whisper, like a voice note). A picture is passed on as it is, or made smaller when it
is too big for the model. Nothing is kept: the pictures go to the model beside the conversation state
(context.attachment_images), for this turn only.

ffmpeg reads files from strangers, so it runs like the code sandbox (app/sandbox.py): as `nobody`, in a private
folder, with CPU, memory and file-size limits, a wall-clock timeout, and only the `file` protocol (a crafted video
can't make it fetch URLs or read other files through playlists).
"""
from __future__ import annotations

import json
import os
import resource
import shutil
import subprocess
import tempfile
from pathlib import Path

from app import sandbox

MAX_FRAMES = 16
FRAME_WIDTH = 1024
AUDIO_SECONDS = 180  # what Whisper hears of a video: reels are shorter; voice notes have the same limit
IMAGE_MAX_BYTES = 3_700_000  # Claude takes up to 5 MB of base64 per picture
IMAGE_MAX_SIDE = 1568  # bigger pictures are scaled down by Claude anyway
WALL_SECONDS = 90
CPU_SECONDS = 120
MEMORY_BYTES = 2 * 1024 ** 3
FILE_BYTES = 64 * 1024 ** 2
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")


class MediaError(ValueError):
    """Shown to the person as it is."""


def _limits() -> None:  # runs in the child, before ffmpeg starts
    for limit, value in ((resource.RLIMIT_CPU, CPU_SECONDS), (resource.RLIMIT_AS, MEMORY_BYTES),
                         (resource.RLIMIT_FSIZE, FILE_BYTES), (resource.RLIMIT_CORE, 0),
                         (resource.RLIMIT_NOFILE, 256)):
        resource.setrlimit(limit, (value, value))


class _Workspace:
    """A folder only `nobody` can use, deleted afterwards."""

    def __enter__(self) -> _Workspace:
        self.uid, self.gid = sandbox._owner()
        sandbox.BASE.mkdir(mode=0o711, parents=True, exist_ok=True)
        os.chmod(sandbox.BASE, 0o711)
        self.dir = Path(tempfile.mkdtemp(prefix="media-", dir=sandbox.BASE))
        os.chown(self.dir, self.uid, self.gid)
        os.chmod(self.dir, 0o700)
        return self

    def put(self, name: str, data: bytes) -> str:
        path = self.dir / name
        path.write_bytes(data)
        os.chown(path, self.uid, self.gid)
        return name

    def run(self, args: list[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                args, cwd=self.dir, env={"PATH": "/usr/bin:/bin", "HOME": str(self.dir), "LANG": "C.UTF-8"},
                stdin=subprocess.DEVNULL, capture_output=True, timeout=WALL_SECONDS, user=self.uid,
                group=self.gid, extra_groups=[], preexec_fn=_limits, close_fds=True, check=False,
            )
        except subprocess.TimeoutExpired:
            raise MediaError("That video took too long to read.") from None

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


_SAFE = ["-nostdin", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "file"]


def _seconds(ws: _Workspace, name: str) -> float:
    out = ws.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-show_entries", "format=duration",
                  "-of", "json", name])
    try:
        return max(0.0, float(json.loads(out.stdout or b"{}")["format"]["duration"]))
    except (KeyError, ValueError, TypeError):
        raise MediaError("I couldn't read that video. Send it as a normal WhatsApp video (MP4).") from None


def video(data: bytes) -> tuple[list[dict], bytes | None, float]:
    """(frames, the audio as Ogg/Opus or None, length in seconds) of a video."""
    with _Workspace() as ws:
        name = ws.put("in.mp4", data)
        seconds = _seconds(ws, name)
        # Frames spread evenly: one every `step` seconds, starting a little in (the first frame is often black).
        step = max(seconds / MAX_FRAMES, 0.5) if seconds else 1.0
        out = ws.run(["ffmpeg", *_SAFE, "-f", "mov", "-i", name, "-an",
                      "-vf", f"fps=1/{step:.3f},scale='min({FRAME_WIDTH},iw)':-2",
                      "-frames:v", str(MAX_FRAMES), "-q:v", "5", "frame-%02d.jpg"])
        frames = sorted(ws.dir.glob("frame-*.jpg"))
        if out.returncode != 0 and not frames:
            raise MediaError("I couldn't read that video. Send it as a normal WhatsApp video (MP4).")
        pictures = []
        for n, path in enumerate(frames):
            at = int(n * step)
            pictures.append({"label": f"frame {n + 1} at {at // 60}:{at % 60:02d}", "data": path.read_bytes(),
                             "type": "image/jpeg"})
        sound = ws.run(["ffmpeg", *_SAFE, "-f", "mov", "-i", name, "-vn", "-t", str(AUDIO_SECONDS), "-ac", "1",
                        "-ar", "16000", "-c:a", "libopus", "-b:a", "24k", "audio.ogg"])
        audio_path = ws.dir / "audio.ogg"
        audio = audio_path.read_bytes() if sound.returncode == 0 and audio_path.exists() else None
        return pictures, (audio if audio and len(audio) > 2000 else None), seconds


def image(data: bytes, mime: str) -> dict:
    """A picture the model can take: as sent when it's small enough, else a smaller JPEG."""
    mime = (mime or "").split(";")[0].strip().lower()
    if mime in IMAGE_TYPES and len(data) <= IMAGE_MAX_BYTES:
        return {"label": "picture", "data": data, "type": mime}
    with _Workspace() as ws:
        name = ws.put("in.img", data)
        out = ws.run(["ffmpeg", *_SAFE, "-i", name, "-frames:v", "1",
                      "-vf", f"scale='min({IMAGE_MAX_SIDE},iw)':-2", "-q:v", "4", "out.jpg"])
        path = ws.dir / "out.jpg"
        if out.returncode != 0 or not path.exists() or path.stat().st_size > IMAGE_MAX_BYTES:
            raise MediaError("I couldn't read that picture. Send it as a normal WhatsApp photo.")
        return {"label": "picture", "data": path.read_bytes(), "type": "image/jpeg"}
