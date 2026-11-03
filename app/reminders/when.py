"""Turn "tomorrow 9am", "in 2 hours", "friday 17:30" or "2026-11-05 09:00" into a moment.

Times are read in the person's timezone and returned in UTC. Errors are ValueErrors whose
message tells the model what to send instead.
"""
from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

DEFAULT_HOUR = 9  # a date with no time means the morning
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_UNITS = {
    "minute": 60, "min": 60, "mins": 60, "minutes": 60, "m": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600, "h": 3600,
    "day": 86400, "days": 86400, "d": 86400,
    "week": 604800, "weeks": 604800, "w": 604800,
}
_TIME = re.compile(r"(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?$")
HELP = ("use a date and time like '2026-11-05 09:00', or 'tomorrow 9am', 'in 2 hours', "
        "'friday 17:30', 'next monday'")


def _clock(text: str) -> tuple[int, int] | None:
    text = text.strip().lower()
    if text in ("", "morning"):
        return None if text == "" else (9, 0)
    named = {"noon": (12, 0), "midday": (12, 0), "afternoon": (15, 0), "evening": (18, 0),
             "night": (21, 0), "tonight": (21, 0), "midnight": (0, 0)}
    if text in named:
        return named[text]
    m = _TIME.fullmatch(text)
    if not m:
        raise ValueError(f"I can't read the time '{text}': {HELP}")
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(".", "")
    if half == "pm" and hour < 12:
        hour += 12
    elif half == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        raise ValueError(f"'{text}' is not a valid time: {HELP}")
    return hour, minute


def parse(when: str, now: dt.datetime, zone: str) -> dt.datetime:
    """`now` is an aware datetime. Returns an aware UTC datetime in the future."""
    tz = ZoneInfo(zone)
    local_now = now.astimezone(tz)
    text = " ".join((when or "").lower().replace(",", " ").split())
    if not text:
        raise ValueError(f"say when: {HELP}")
    text = re.sub(r"^(on|at)\s+", "", text)

    result: dt.datetime | None = None
    # in 10 minutes / in 2 hours and 30 minutes / in 1.5 hours
    if text.startswith("in "):
        total = 0.0
        for amount, unit in re.findall(r"(\d+(?:\.\d+)?|an?|half an?)\s*([a-z]+)", text[3:]):
            seconds = _UNITS.get(unit)
            if seconds is None:
                raise ValueError(f"I can't read '{when}': {HELP}")
            n = 0.5 if amount.startswith("half") else 1.0 if amount in ("a", "an") else float(amount)
            total += n * seconds
        if total <= 0:
            raise ValueError(f"I can't read '{when}': {HELP}")
        return (now + dt.timedelta(seconds=total)).astimezone(dt.timezone.utc)

    # ISO date, optionally with a time
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:[ t](.+))?", text)
    if m:
        try:
            day = dt.date.fromisoformat(m.group(1))
        except ValueError:
            raise ValueError(f"'{m.group(1)}' is not a real date: {HELP}") from None
        rest = (m.group(2) or "").strip()
        offset = re.fullmatch(r"(\d{1,2}:\d{2})(?::\d{2})?(?:\.\d+)?(z|[+-]\d{2}:?\d{2})", rest)
        if offset:  # an explicit UTC offset wins over the person's timezone
            stamp = f"{m.group(1)}T{offset.group(1).zfill(5)}{offset.group(2).replace('z', '+00:00')}"
            result = dt.datetime.fromisoformat(stamp)
        else:
            rest = re.sub(r"^(\d{1,2}:\d{2}):\d{2}(\.\d+)?$", r"\1", rest)
            hm = _clock(rest) or (DEFAULT_HOUR, 0)
            result = dt.datetime.combine(day, dt.time(*hm), tz)
    else:
        words = text.split(" ", 1)
        head, rest = words[0], words[1] if len(words) > 1 else ""
        if head in ("today", "tonight"):
            hm = _clock(rest) or ((21, 0) if head == "tonight" else None)
            if hm is None:
                raise ValueError(f"what time today? {HELP}")
            result = dt.datetime.combine(local_now.date(), dt.time(*hm), tz)
        elif head == "tomorrow":
            hm = _clock(rest) or (DEFAULT_HOUR, 0)
            result = dt.datetime.combine(local_now.date() + dt.timedelta(days=1), dt.time(*hm), tz)
        else:
            nxt = head == "next"
            if nxt:
                head, _, rest = rest.partition(" ")
            day_name = next((d for d in WEEKDAYS if d.startswith(head) and len(head) >= 3), None)
            if day_name is not None:
                ahead = (WEEKDAYS.index(day_name) - local_now.weekday()) % 7
                hm = _clock(rest) or (DEFAULT_HOUR, 0)
                candidate = dt.datetime.combine(local_now.date() + dt.timedelta(days=ahead), dt.time(*hm), tz)
                if candidate <= local_now or (nxt and ahead == 0):
                    candidate += dt.timedelta(days=7)
                result = candidate
            elif head == "week" and nxt:
                ahead = 7 - local_now.weekday()
                result = dt.datetime.combine(local_now.date() + dt.timedelta(days=ahead), dt.time(DEFAULT_HOUR), tz)
            else:
                hm = _clock(text)  # just a time: the next time it comes round
                if hm is None:
                    raise ValueError(f"I can't read '{when}': {HELP}")
                result = dt.datetime.combine(local_now.date(), dt.time(*hm), tz)
                if result <= local_now:
                    result += dt.timedelta(days=1)

    if result <= now:
        raise ValueError(f"'{when}' is in the past (it is now {local_now:%Y-%m-%d %H:%M} {zone})")
    return result.astimezone(dt.timezone.utc)


def next_occurrence(due: dt.datetime, repeat: str, zone: str) -> dt.datetime | None:
    """The following time a repeating reminder is due, keeping the same local clock time."""
    if repeat in ("", "none"):
        return None
    tz = ZoneInfo(zone)
    local = due.astimezone(tz)
    if repeat == "daily":
        nxt = local + dt.timedelta(days=1)
    elif repeat == "weekly":
        nxt = local + dt.timedelta(days=7)
    elif repeat == "weekdays":
        nxt = local + dt.timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += dt.timedelta(days=1)
    elif repeat == "monthly":
        month = local.month % 12 + 1
        year = local.year + (local.month == 12)
        day = local.day
        while True:
            try:
                nxt = local.replace(year=year, month=month, day=day)
                break
            except ValueError:
                day -= 1
    else:
        return None
    # Rebuild from wall-clock fields so daylight-saving changes keep the same local time.
    nxt = dt.datetime.combine(nxt.date(), local.time().replace(tzinfo=None), tz)
    return nxt.astimezone(dt.timezone.utc)


def describe(moment: dt.datetime, zone: str) -> str:
    return moment.astimezone(ZoneInfo(zone)).strftime("%a %-d %b %Y, %H:%M")
