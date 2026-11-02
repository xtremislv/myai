"""The current date and time, for agents' instructions. Without it a model guesses from its
training data ("tomorrow, May 23, 2024")."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from app.config import get_settings


def now_line(now: dt.datetime | None = None) -> str:
    zone = get_settings().timezone
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(zone))
    return (
        f"Today is {now:%A, %-d %B %Y} and the time is {now:%H:%M} ({zone}). Work out dates such "
        "as 'tomorrow' or 'next Monday' from this, and state the actual date when it matters."
    )


def with_now(prompt: str) -> str:
    return f"{prompt}\n\n{now_line()}"
