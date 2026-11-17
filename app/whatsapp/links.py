"""Who is who on WhatsApp, the bot's conversation key, and each person's result page.

Linking: the person enters their number in Settings and gets a one-time code; they send "LINK <code>" to the bot
from that number. WhatsApp vouches for the sender's number, so the code proves the number is theirs. Only linked,
active accounts get answers (the allowlist).

The conversation key: the person's own chat key opens only with their password or their phone app, neither of
which is on WhatsApp. The bot's conversation (and the result page) is encrypted with a key derived from the
server's notes key (app/vault/notes.py), like reminders: the database or a backup alone can't read it, but the
server can. Their encrypted app chats, My files, own mailbox and saved details stay app-only (owner, 2026-11-17).

The result page: one secret link per person, /r/<token>, showing their latest long answer or dashboard (owner's
choice: the link alone opens it, no sign-in). The token is derived from the server key and `page_version`; only its
hash is stored, so a database copy can't rebuild the link. Resetting the link changes the version. Kept PAGE_DAYS.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import re
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import WhatsAppLink
from app.vault import notes

CODE_MINUTES = 15
PAGE_DAYS = 7
WINDOW = dt.timedelta(hours=23, minutes=50)  # WhatsApp's 24 hours, with a margin
DEFAULT_COUNTRY = "91"  # a 10-digit number is taken as Indian


class LinkError(ValueError):
    """Shown to the person as is."""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def normalize_phone(raw: str) -> str:
    """Digits with the country code, as WhatsApp sends them ("919812345678")."""
    text = (raw or "").strip()
    digits = re.sub(r"\D", "", text)
    if text.startswith("00"):
        digits = digits[2:]
    elif not text.startswith("+"):
        if len(digits) == 11 and digits.startswith("0"):
            digits = DEFAULT_COUNTRY + digits[1:]
        elif len(digits) == 10:
            digits = DEFAULT_COUNTRY + digits
    if not 11 <= len(digits) <= 15 or digits.startswith("0"):
        raise LinkError("Enter the WhatsApp number with its country code, e.g. +91 98123 45678.")
    return digits


def _code_hash(user_id: str, code: str) -> str:
    return hashlib.sha256(f"{user_id}:{code}".encode()).hexdigest()


def get(session: Session, user_id: str) -> WhatsAppLink | None:
    return session.get(WhatsAppLink, user_id)


def by_phone(session: Session, phone: str) -> WhatsAppLink | None:
    return session.query(WhatsAppLink).filter(WhatsAppLink.phone == phone).one_or_none()


def start(session: Session, user_id: str, raw_phone: str) -> str:
    """A new one-time code for linking this number to this account."""
    phone = normalize_phone(raw_phone)
    other = by_phone(session, phone)
    if other is not None and other.user_id != user_id:
        raise LinkError("That number is already linked to another account.")
    row = get(session, user_id) or WhatsAppLink(user_id=user_id, thread_version=1, notify=True, page_version=1)
    code = f"{secrets.randbelow(10**6):06d}"
    row.pending_phone, row.code_hash = phone, _code_hash(user_id, code)
    row.code_expires_at = _now() + dt.timedelta(minutes=CODE_MINUTES)
    session.add(row)
    session.commit()
    return code


def confirm(session: Session, phone: str, code: str) -> WhatsAppLink | None:
    """The link this code (sent from this number) completes, or None."""
    now = _now()
    for row in session.query(WhatsAppLink).filter(WhatsAppLink.pending_phone == phone).all():
        expires = _aware(row.code_expires_at)
        if row.code_hash and expires and expires > now and hmac.compare_digest(row.code_hash, _code_hash(row.user_id, code)):
            row.phone, row.linked_at, row.last_inbound_at = phone, now, now
            row.pending_phone = row.code_hash = row.code_expires_at = None
            try:
                session.commit()
            except IntegrityError:  # linked to someone else in the meantime
                session.rollback()
                return None
            return row
    return None


def unlink(session: Session, user_id: str) -> bool:
    row = get(session, user_id)
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


def touch(session: Session, row: WhatsAppLink) -> None:
    """The person just wrote: the 24-hour window starts again."""
    row.last_inbound_at = _now()
    session.commit()


def in_window(row: WhatsAppLink, now: dt.datetime | None = None) -> bool:
    last = _aware(row.last_inbound_at)
    return last is not None and (now or _now()) - last < WINDOW


def thread_id(row: WhatsAppLink) -> str:
    return f"whatsapp-{row.thread_version}"


def is_whatsapp_thread(thread: str | None) -> bool:
    return bool(thread) and thread.startswith("whatsapp-")


# ---- keys ----
def _derive(session: Session, purpose: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose.encode()).derive(notes._key(session))


def chat_key(session: Session, user_id: str) -> bytes:
    """The key the bot's conversation with this person is encrypted with (see the module docstring)."""
    return _derive(session, f"whatsapp-chat:{user_id}")


# ---- the result page ----
def _token(session: Session, row: WhatsAppLink) -> str:
    mac = hmac.new(_derive(session, "whatsapp-page"), f"{row.user_id}:{row.page_version}".encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")[:32]


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def base_url() -> str:
    return (get_settings().public_app_url or "").rstrip("/")


def page_url(session: Session, row: WhatsAppLink) -> str:
    token = _token(session, row)
    if row.page_token_hash != _token_hash(token):
        row.page_token_hash = _token_hash(token)
        session.commit()
    return f"{base_url()}/r/{token}"


def save_page(session: Session, row: WhatsAppLink, title: str, markdown: str, dashboards: list[dict]) -> str:
    """Replace the person's page with this answer; returns its (unchanged) link."""
    blob = json.dumps({"title": title[:200], "markdown": markdown, "dashboards": dashboards or []})
    row.page = notes.encrypt(session, blob, f"whatsapp-page:{row.user_id}")
    row.page_updated_at = _now()
    session.commit()
    return page_url(session, row)


def load_page(session: Session, token: str) -> dict | None:
    if not token or len(token) > 64:
        return None
    row = session.query(WhatsAppLink).filter(WhatsAppLink.page_token_hash == _token_hash(token)).one_or_none()
    if row is None or not hmac.compare_digest(_token(session, row), token):
        return None
    updated = _aware(row.page_updated_at)
    if row.page is None or updated is None or _now() - updated > dt.timedelta(days=PAGE_DAYS):
        return {"title": "", "markdown": "", "dashboards": [], "updated_at": None}
    data = json.loads(notes.decrypt(session, row.page, f"whatsapp-page:{row.user_id}"))
    return {**data, "updated_at": updated.isoformat()}


def reset_page(session: Session, row: WhatsAppLink) -> str:
    """A new link: the old one stops working at once."""
    row.page_version += 1
    row.page = None
    row.page_updated_at = None
    session.commit()
    return page_url(session, row)


def purge_pages(session: Session, now: dt.datetime | None = None) -> int:
    """Delete result pages older than PAGE_DAYS (housekeeping)."""
    cutoff = (now or _now()) - dt.timedelta(days=PAGE_DAYS)
    rows = session.query(WhatsAppLink).filter(WhatsAppLink.page.is_not(None), WhatsAppLink.page_updated_at < cutoff).all()
    for row in rows:
        row.page, row.page_updated_at = None, None
    session.commit()
    return len(rows)
