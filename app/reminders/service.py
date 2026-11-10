"""Reminders and notifications (the bell in the web UI), and delivering them when due.

Text is encrypted with the server's notes key (app/vault/notes.py): the scheduler must read
a reminder when it falls due, while its owner may be signed out.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import Notification, Reminder, User
from app.reminders import when as when_
from app.vault import notes

logger = logging.getLogger(__name__)

REPEATS = ("none", "daily", "weekdays", "weekly", "monthly")
MAX_TEXT = 500
MAX_PENDING = 200
KEEP_NOTIFICATIONS_DAYS = 90


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


def zone_of(user: User | None) -> str:
    zone = ((user.prefs or {}) if user is not None else {}).get("timezone")
    return zone or get_settings().timezone


def _text_ctx(reminder_id: str) -> str:
    return f"reminder:{reminder_id}"


# ---- reminders ----
def create(
    session: Session, owner_id: str, when: str, text: str, *, repeat: str = "none", email: bool = False,
    source: str = "chat", source_ref: str | None = None, now: dt.datetime | None = None,
) -> tuple[Reminder, str]:
    """Returns the reminder and its due time as the person would read it."""
    text = " ".join((text or "").split())[:MAX_TEXT]
    if not text:
        raise ValueError("say what the reminder is for")
    repeat = (repeat or "none").lower().strip()
    if repeat not in REPEATS:
        raise ValueError(f"repeat must be one of: {', '.join(REPEATS)}")
    pending = session.query(Reminder).filter(Reminder.owner_id == owner_id, Reminder.state == "pending").count()
    if pending >= MAX_PENDING:
        raise ValueError(f"you already have {MAX_PENDING} reminders waiting: cancel some first")
    zone = zone_of(session.get(User, owner_id))
    due = when_.parse(when, now or _now(), zone)
    row = Reminder(
        id=uuid.uuid4().hex, owner_id=owner_id, due_at=due, repeat=repeat, email=email,
        source=source, source_ref=source_ref, state="pending",
    )
    row.encrypted_text = notes.encrypt(session, text, _text_ctx(row.id))
    session.add(row)
    session.commit()
    return row, when_.describe(due, zone)


def text_of(session: Session, row: Reminder) -> str:
    return notes.decrypt(session, row.encrypted_text, _text_ctx(row.id))


def pending(session: Session, owner_id: str) -> list[dict]:
    zone = zone_of(session.get(User, owner_id))
    rows = (
        session.query(Reminder)
        .filter(Reminder.owner_id == owner_id, Reminder.state == "pending")
        .order_by(Reminder.due_at)
        .all()
    )
    return [
        {
            "id": r.id, "text": text_of(session, r), "due_at": _aware(r.due_at),
            "due": when_.describe(_aware(r.due_at), zone), "repeat": r.repeat, "email": r.email,
            "source": r.source,
        }
        for r in rows
    ]


def cancel(session: Session, owner_id: str, reminder_id: str) -> bool:
    """Accepts the full id or its first 8 characters (as the agent shows them)."""
    query = session.query(Reminder).filter(Reminder.owner_id == owner_id, Reminder.state == "pending")
    rows = [r for r in query.all() if r.id == reminder_id or (len(reminder_id) >= 6 and r.id.startswith(reminder_id))]
    if len(rows) != 1:
        return False
    rows[0].state = "cancelled"
    session.commit()
    return True


def has_source(session: Session, owner_id: str, source_ref: str) -> bool:
    return session.query(Reminder).filter(
        Reminder.owner_id == owner_id, Reminder.source_ref == source_ref
    ).first() is not None


# ---- notifications ----
def notify(session: Session, owner_id: str, kind: str, title: str, body: str = "") -> Notification:
    row = Notification(id=uuid.uuid4().hex, owner_id=owner_id, kind=kind, created_at=_now())
    row.encrypted_title = notes.encrypt(session, title[:300], f"notification:{row.id}:title")
    row.encrypted_body = notes.encrypt(session, body[:20000], f"notification:{row.id}:body")
    session.add(row)
    session.commit()
    from app import push

    push.wake(session, owner_id, "notification", kind=kind, id=row.id)  # the phone app fetches it
    return row


def notify_admins(session: Session, kind: str, title: str, body: str = "") -> int:
    admins = [u for u in session.query(User).all() if "admin" in (u.roles or []) and not u.disabled]
    for user in admins:
        notify(session, user.id, kind, title, body)
    return len(admins)


def notifications(session: Session, owner_id: str, *, unread_only: bool = False, limit: int = 50) -> list[dict]:
    query = session.query(Notification).filter(Notification.owner_id == owner_id)
    if unread_only:
        query = query.filter(Notification.read_at.is_(None))
    rows = query.order_by(Notification.created_at.desc()).limit(limit).all()
    return [
        {
            "id": r.id, "kind": r.kind, "created_at": _aware(r.created_at), "read": r.read_at is not None,
            "title": notes.decrypt(session, r.encrypted_title, f"notification:{r.id}:title"),
            "body": notes.decrypt(session, r.encrypted_body, f"notification:{r.id}:body"),
        }
        for r in rows
    ]


def unread_count(session: Session, owner_id: str) -> int:
    return session.query(Notification).filter(
        Notification.owner_id == owner_id, Notification.read_at.is_(None)
    ).count()


def mark_read(session: Session, owner_id: str, notification_id: str | None = None) -> int:
    query = session.query(Notification).filter(Notification.owner_id == owner_id, Notification.read_at.is_(None))
    if notification_id:
        query = query.filter(Notification.id == notification_id)
    rows = query.all()
    for row in rows:
        row.read_at = _now()
    session.commit()
    return len(rows)


def prune_notifications(session: Session, now: dt.datetime | None = None) -> int:
    cutoff = (now or _now()) - dt.timedelta(days=KEEP_NOTIFICATIONS_DAYS)
    rows = session.query(Notification).filter(Notification.read_at.is_not(None)).all()
    old = [r for r in rows if _aware(r.created_at) < cutoff]
    for row in old:
        session.delete(row)
    session.commit()
    return len(old)


# ---- delivery ----
Emailer = Callable[[Session, User, str, str], bool]


def deliver_due(session: Session, now: dt.datetime | None = None, emailer: Emailer | None = None) -> int:
    """Turn every reminder that has come due into a notification (and an email if asked),
    then schedule its next repeat or mark it sent. Returns how many were delivered."""
    from app.reminders.delivery import email_person

    now = now or _now()
    emailer = emailer or email_person
    due = (
        session.query(Reminder)
        .filter(Reminder.state == "pending", Reminder.due_at <= now)
        .order_by(Reminder.due_at)
        .limit(500)
        .all()
    )
    for row in due:
        user = session.get(User, row.owner_id)
        if user is None or user.disabled:
            row.state = "cancelled"
            session.commit()
            continue
        text = text_of(session, row)
        notify(session, row.owner_id, "reminder", text, f"Reminder set for {when_.describe(_aware(row.due_at), zone_of(user))}")
        if row.email or (user.prefs or {}).get("email_reminders"):
            try:
                emailer(session, user, f"Reminder: {text[:80]}", text)
            except Exception as e:  # noqa: BLE001 - the in-app reminder already went out
                logger.warning("reminder email failed: %s", type(e).__name__)
        nxt = when_.next_occurrence(_aware(row.due_at), row.repeat, zone_of(user))
        while nxt is not None and nxt <= now:  # the server was down: skip missed repeats
            nxt = when_.next_occurrence(nxt, row.repeat, zone_of(user))
        row.sent_at = now
        if nxt is None:
            row.state = "sent"
        else:
            row.due_at = nxt
        session.commit()
    return len(due)


def erase_owner(session: Session, owner_id: str) -> int:
    n = session.query(Reminder).filter(Reminder.owner_id == owner_id).delete()
    n += session.query(Notification).filter(Notification.owner_id == owner_id).delete()
    session.commit()
    return n
