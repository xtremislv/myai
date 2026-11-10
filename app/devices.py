"""Signed-in phones (the Android app).

The web app renews its token from an HttpOnly cookie while it is open. A phone app is closed
most of the time and must still wake up to show reminders, so it keeps a refresh token instead.

A refresh token is "<rotating part>.<device secret>":
  - the rotating part authenticates the phone. Only its SHA-256 is stored, it is replaced on
    every refresh, and the one before is accepted once more (a phone that lost the answer to a
    refresh over a bad connection isn't signed out);
  - the device secret never changes for that sign-in and is never stored. The person's chat
    key is kept wrapped by a key derived from it, so the database, backups and the vault master
    key can't open private chats from a device row: as with the password, it takes something
    only the phone holds.

Revoking a device (the person, or the admin resetting their password) makes its refresh token
useless at once; an access token it already has lasts until it expires (JWT_TTL_SECONDS).
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import os
import secrets
import uuid

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy.orm import Session

from app.db.session import DeviceSession, User

MAX_DEVICES = 10  # per person; the oldest unused one is signed out to make room
PLATFORMS = ("android",)


class DeviceError(Exception):
    """The refresh token is wrong, or the device was signed out."""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hash(rotating: str) -> str:
    return hashlib.sha256(rotating.encode()).hexdigest()


def _wrapping_key(device_secret: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"tw-device-chat-key").derive(device_secret)


def _aad(row: DeviceSession) -> bytes:
    return f"device:{row.id}:{row.owner_id}".encode()


def _split(refresh_token: str) -> tuple[str, bytes]:
    try:
        rotating, secret = refresh_token.strip().split(".", 1)
        device_secret = _unb64(secret)
    except (ValueError, TypeError) as e:
        raise DeviceError("malformed refresh token") from e
    if not rotating or len(device_secret) != 32:
        raise DeviceError("malformed refresh token")
    return rotating, device_secret


def _issue(row: DeviceSession, chat_key: bytes | None, device_secret: bytes | None = None) -> str:
    """A new rotating part (and, when given no device secret, a new secret with the chat key
    wrapped again under it). Returns the refresh token for the phone."""
    rotating = secrets.token_urlsafe(32)
    row.prev_refresh_hash = None
    row.refresh_hash = _hash(rotating)
    if device_secret is None:
        device_secret = os.urandom(32)
        if chat_key is None:
            row.key_nonce = row.wrapped_chat_key = None
        else:
            row.key_nonce = os.urandom(12)
            row.wrapped_chat_key = AESGCM(_wrapping_key(device_secret)).encrypt(row.key_nonce, chat_key, _aad(row))
    return f"{rotating}.{_b64(device_secret)}"


def register(
    session: Session, user: User, chat_key: bytes | None, *, name: str = "", platform: str = "android",
    push_token: str | None = None, replace_id: str | None = None,
) -> tuple[DeviceSession, str]:
    """Sign a phone in (or, with replace_id, sign the same phone in again, e.g. after the
    person set their own password and now has a chat key). Returns the row and refresh token."""
    row = None
    if replace_id:
        row = session.get(DeviceSession, replace_id)
        if row is None or row.owner_id != user.id:
            raise DeviceError("no such device")
        row.revoked_at = None
    if row is None:
        live = (
            session.query(DeviceSession)
            .filter(DeviceSession.owner_id == user.id, DeviceSession.revoked_at.is_(None))
            .all()
        )
        live.sort(key=lambda r: _aware(r.last_used_at or r.created_at))
        for old in live[: max(0, len(live) - MAX_DEVICES + 1)]:
            old.revoked_at = _now()
        row = DeviceSession(id=uuid.uuid4().hex, owner_id=user.id, platform=platform, refresh_hash="")
        session.add(row)
    row.name = (name or "").strip()[:128] or "Android phone"
    row.platform = platform
    if push_token is not None:
        row.push_token = push_token.strip()[:512] or None
    row.last_used_at = _now()
    refresh_token = _issue(row, chat_key)
    session.commit()
    return row, refresh_token


def refresh(session: Session, device_id: str, refresh_token: str) -> tuple[User, DeviceSession, bytes | None, str]:
    """Check a refresh token and replace it. Returns the user, the device, their chat key (None
    until they have one) and the new refresh token."""
    rotating, device_secret = _split(refresh_token)
    row = session.get(DeviceSession, device_id)
    if row is None or row.revoked_at is not None:
        raise DeviceError("this device was signed out")
    presented = _hash(rotating)
    current = hmac.compare_digest(presented, row.refresh_hash or "")
    previous = bool(row.prev_refresh_hash) and hmac.compare_digest(presented, row.prev_refresh_hash)
    if not (current or previous):
        raise DeviceError("this refresh token is no longer valid")
    user = session.get(User, row.owner_id)
    if user is None or user.disabled:
        raise DeviceError("this account can't sign in")
    chat_key = None
    if row.wrapped_chat_key is not None:
        try:
            chat_key = AESGCM(_wrapping_key(device_secret)).decrypt(row.key_nonce, row.wrapped_chat_key, _aad(row))
        except Exception as e:  # noqa: BLE001 - InvalidTag: wrong device secret
            raise DeviceError("this refresh token is no longer valid") from e
    old_hash = row.refresh_hash
    new_token = _issue(row, chat_key, device_secret)
    # The token just replaced stays usable once, in case this answer never reaches the phone.
    row.prev_refresh_hash = old_hash if current else None
    row.last_used_at = _now()
    session.commit()
    return user, row, chat_key, new_token


def list_for(session: Session, owner_id: str) -> list[DeviceSession]:
    return (
        session.query(DeviceSession)
        .filter(DeviceSession.owner_id == owner_id, DeviceSession.revoked_at.is_(None))
        .order_by(DeviceSession.created_at.desc())
        .all()
    )


def revoke(session: Session, owner_id: str, device_id: str) -> bool:
    row = session.get(DeviceSession, device_id)
    if row is None or row.owner_id != owner_id or row.revoked_at is not None:
        return False
    row.revoked_at = _now()
    row.push_token = None
    row.wrapped_chat_key = row.key_nonce = None
    session.commit()
    return True


def revoke_all(session: Session, owner_id: str) -> int:
    """Sign every phone of this person out (their chat key changed, or their account was reset)."""
    rows = list_for(session, owner_id)
    for row in rows:
        row.revoked_at = _now()
        row.push_token = None
        row.wrapped_chat_key = row.key_nonce = None
    session.commit()
    return len(rows)


def set_push_token(session: Session, owner_id: str, device_id: str, push_token: str | None) -> bool:
    row = session.get(DeviceSession, device_id)
    if row is None or row.owner_id != owner_id or row.revoked_at is not None:
        return False
    token = (push_token or "").strip()[:512] or None
    if token:
        # A token belongs to one app install: drop it from any other row (a re-install, another account).
        for other in session.query(DeviceSession).filter(DeviceSession.push_token == token).all():
            if other.id != row.id:
                other.push_token = None
    row.push_token = token
    session.commit()
    return True


def push_tokens(session: Session, owner_id: str) -> list[str]:
    return [r.push_token for r in list_for(session, owner_id) if r.push_token]


def forget_push_token(session: Session, token: str) -> None:
    """FCM said this token is gone (app uninstalled)."""
    for row in session.query(DeviceSession).filter(DeviceSession.push_token == token).all():
        row.push_token = None
    session.commit()


def erase_owner(session: Session, owner_id: str) -> int:
    count = session.query(DeviceSession).filter(DeviceSession.owner_id == owner_id).delete()
    session.commit()
    return count


def _aware(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value
