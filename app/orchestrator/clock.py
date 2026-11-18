"""The current date and time, for agents' instructions. Without it a model guesses from its
training data ("tomorrow, May 23, 2024").

The time changes every minute, so it must not sit in the system prompt: the chat template puts
the tool definitions (several thousand tokens) right after the system text, and a changed minute
there makes vLLM recompute all of them (~6 s on this GPU) instead of reusing its prefix cache.
`with_now` therefore adds only the fixed rules plus a marker, and `place_clock` (called by
app/llm/client.py for every request) swaps the marker for a short time stamp on the latest user
message, after everything that can be cached.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
import time
from zoneinfo import ZoneInfo

from app.config import get_settings

# Never reaches the model: place_clock removes it.
CLOCK_MARK = "⁣[[clock]]⁣"

DATE_RULES = (
    "The latest user message ends with the current date and time as [Now: ...]. The system adds "
    "it, not the person: don't mention or repeat it. Work out dates such as 'tomorrow' or 'next "
    "Monday' from it, and state the actual date when it matters. It is the real date now, not a "
    "hypothetical: your training data ends earlier, so anything since then is known only from "
    "tools, and dated results up to today are real, current events. Never say a date is in the "
    "future or that no information can exist."
)


def _local(now: dt.datetime | None) -> tuple[dt.datetime, str]:
    zone = get_settings().timezone
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(zone)), zone


def now_line(now: dt.datetime | None = None) -> str:
    """The date and time as a full instruction, for prompts without a user turn to carry it."""
    now, zone = _local(now)
    return (
        f"Today is {now:%A, %-d %B %Y} and the time is {now:%H:%M} ({zone}). Work out dates such "
        "as 'tomorrow' or 'next Monday' from this, and state the actual date when it matters. "
        "This is the real date now, not a hypothetical: your training data ends earlier, so "
        "anything since then is known only from tools, and dated results up to today are real, "
        "current events. Never say a date is in the future or that no information can exist."
    )


def now_stamp(now: dt.datetime | None = None) -> str:
    now, zone = _local(now)
    return f"[Now: {now:%A, %-d %B %Y}, {now:%H:%M} ({zone})]"


def with_now(prompt: str) -> str:
    return f"{prompt}\n\n{DATE_RULES}{CLOCK_MARK}"


def _stamped(content, stamp: str):
    if isinstance(content, list):  # multimodal parts
        return [*content, {"type": "text", "text": stamp}]
    return f"{content or ''}\n\n{stamp}"


# One stamp per turn: every tool round of a turn sends the stamp its first call sent, so the turn's
# requests keep the same text and read each other back from the prompt cache (a minute ticking
# over mid-turn changed it). Kept TURN_STAMP_SECONDS at most, so a very long run still moves on.
TURN_STAMP_SECONDS = 600
_turn_stamps: dict[str, tuple[str, float]] = {}
_turn_lock = threading.Lock()


def _turn_key(messages: list[dict], last_user: int) -> str:
    """The conversation up to and including the person's latest message (no system prompt)."""
    convo = [m for m in messages[:last_user + 1] if m.get("role") != "system"]
    return hashlib.sha256(json.dumps(convo, sort_keys=True, default=str).encode()).hexdigest()


def turn_stamp(messages: list[dict], last_user: int) -> str:
    key, now = _turn_key(messages, last_user), time.monotonic()
    with _turn_lock:
        for k in [k for k, (_, t) in _turn_stamps.items() if now - t > TURN_STAMP_SECONDS]:
            del _turn_stamps[k]
        if key not in _turn_stamps:
            _turn_stamps[key] = (now_stamp(), now)
        return _turn_stamps[key][0]


def place_clock(messages: list[dict], now: dt.datetime | None = None) -> list[dict]:
    """Messages ready to send: the clock marker left by with_now is removed from the system
    prompt and the time goes on the latest user message instead. Without a user message the
    full date line takes the marker's place. The input is not changed."""
    marked = [i for i, m in enumerate(messages)
              if m.get("role") == "system" and CLOCK_MARK in str(m.get("content") or "")]
    if not marked:
        return messages
    users = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    out = list(messages)
    for i in marked:
        content = out[i]["content"]
        if users:
            content = content.replace(CLOCK_MARK, "")
        else:
            content = content.replace(f"{DATE_RULES}{CLOCK_MARK}", now_line(now)).replace(CLOCK_MARK, "")
        out[i] = {**out[i], "content": content}
    if users:
        last = users[-1]
        stamp = now_stamp(now) if now is not None else turn_stamp(messages, last)
        out[last] = {**out[last], "content": _stamped(out[last].get("content"), stamp)}
    return out
