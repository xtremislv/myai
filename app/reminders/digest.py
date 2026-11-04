"""The morning mail digest: read a person's new mail, summarize it, and set reminders for the
deadlines in it.

Runs from the scheduler for everyone who switched it on (prefs.digest_hour, in their own
timezone) and has a personal mailbox connected. Each email's deadline becomes at most one
reminder (keyed by a hash of its Message-ID and deadline), so reruns don't duplicate them.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.db.session import User
from app.mail import connector, summary
from app.reminders import service
from app.reminders import when as when_
from app.vault.aesgcm_vault import AESGCMVault

logger = logging.getLogger(__name__)

MAX_MAIL = 20
LEAD = dt.timedelta(hours=1)  # a deadline with a time: remind this long before


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    return value.replace(tzinfo=dt.timezone.utc) if value is not None and value.tzinfo is None else value


def digest_hour(user: User) -> int | None:
    hour = (user.prefs or {}).get("digest_hour")
    return hour if isinstance(hour, int) and 0 <= hour <= 23 else None


def is_due(user: User, now: dt.datetime) -> bool:
    hour = digest_hour(user)
    if hour is None or user.disabled:
        return False
    local = now.astimezone(ZoneInfo(service.zone_of(user)))
    if local.hour < hour:
        return False
    last = _aware(user.last_digest_at)
    return last is None or last.astimezone(local.tzinfo).date() < local.date()


def _mailbox(session: Session, user: User) -> connector.MailConfig | None:
    raw = AESGCMVault(session).get(connector.secret_name("personal", user.id))
    if raw is None:
        return None
    try:
        return connector.MailConfig.from_secret(raw, guarded=True, name=connector.secret_name("personal", user.id))
    except (ValueError, KeyError):
        return None


def reminder_time(deadline: str, user: User, now: dt.datetime) -> dt.datetime | None:
    """When to remind about a deadline: an hour before a timed one; for a date, the morning
    of the day before (or that morning, if the day before has passed)."""
    zone = service.zone_of(user)
    tz = ZoneInfo(zone)
    try:
        if " " in deadline:
            at = dt.datetime.strptime(deadline, "%Y-%m-%d %H:%M").replace(tzinfo=tz) - LEAD
            options = [at, at + LEAD - dt.timedelta(minutes=10)]
        else:
            day = dt.date.fromisoformat(deadline)
            hour = digest_hour(user) or when_.DEFAULT_HOUR
            options = [dt.datetime.combine(day - dt.timedelta(days=1), dt.time(hour), tz),
                       dt.datetime.combine(day, dt.time(hour), tz)]
    except ValueError:
        return None
    return next((o.astimezone(dt.timezone.utc) for o in options if o > now), None)


def _ref(item: dict) -> str:
    return hashlib.sha256(f"{item['message_id'] or item['id']}|{item['deadline']}".encode()).hexdigest()[:40]


def run_for(
    session: Session, user: User, now: dt.datetime | None = None, *, fetch=None, analyse=None, emailer=None,
    since: dt.datetime | None = None,
) -> str:
    """Build and deliver one person's digest. Returns the notification text."""
    now = now or _now()
    fetch = fetch or connector.fetch_messages
    analyse = analyse or summary.analyse
    cfg = _mailbox(session, user)
    previous = _aware(user.last_digest_at)
    user.last_digest_at = now  # first, so a failure below can't make it retry every minute
    session.commit()
    if cfg is None:
        return ""
    start = since or previous or now - dt.timedelta(days=1)
    start = min(start, now - dt.timedelta(days=1))
    try:
        messages = fetch(cfg, "inbox", MAX_MAIL, True, start.date())
    except Exception as e:  # noqa: BLE001 - report it to the person, not as a crash
        logger.warning("digest: mailbox unreachable: %s", type(e).__name__)
        text = ("I couldn't read your mailbox for today's digest. Check the mailbox settings under "
                "My account (the password may have changed).")
        service.notify(session, user.id, "digest", "Mail digest: mailbox unreachable", text)
        return text

    items = analyse(messages) if messages else []
    created = []
    for item in items:
        if not item["deadline"] or not (item["action"] or item["kind"] in ("event", "travel", "bill")):
            continue
        ref = _ref(item)
        if service.has_source(session, user.id, ref):
            continue
        at = reminder_time(item["deadline"], user, now)
        if at is None:
            continue
        text = f"{item['action'] or item['gist']} (email: {item['subject'][:80]})"
        try:
            _, due = service.create(session, user.id, at.isoformat(), text, source="mail", source_ref=ref, now=now)
        except ValueError:
            continue
        created.append(f"- {due}: {text}")

    actions = sum(1 for i in items if i["action"])
    title = f"Mail digest: {len(items)} new" + (f", {actions} need action" if actions else "")
    body = summary.render(items)
    if created:
        body += "\n\nReminders I set from your mail:\n" + "\n".join(created)
    service.notify(session, user.id, "digest", title, body)
    if (user.prefs or {}).get("digest_email") and items:
        try:
            (emailer or _email)(session, user, title, body)
        except Exception as e:  # noqa: BLE001
            logger.warning("digest email failed: %s", type(e).__name__)
    return body


def _email(session: Session, user: User, subject: str, body: str) -> bool:
    from app.reminders.delivery import email_person

    return email_person(session, user, subject, body)


def run_due(session: Session, now: dt.datetime | None = None) -> int:
    now = now or _now()
    count = 0
    for user in session.query(User).all():
        if is_due(user, now):
            try:
                run_for(session, user, now)
                count += 1
            except Exception:  # noqa: BLE001 - one person's digest must not stop the others
                logger.exception("digest failed")
                session.rollback()
    return count
