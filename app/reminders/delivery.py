"""Emailing a person their reminders and digests.

The address is the one in their settings, or else their connected mailbox's own address.
It is sent from the company's notification mailbox (vault secret `mail/notify`) if there is
one, or else from the person's own mailbox to itself.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.session import User
from app.mail.connector import MailConfig, secret_name, send
from app.vault.aesgcm_vault import AESGCMVault

NOTIFY_SECRET = "mail/notify"


def _config(vault: AESGCMVault, name: str) -> MailConfig | None:
    raw = vault.get(name)
    if raw is None:
        return None
    try:
        cfg = MailConfig.from_secret(raw, guarded=name.startswith("mail/personal/"), name=name)
    except (ValueError, KeyError):
        return None
    return cfg if cfg.smtp_host else None


def address_of(session: Session, user: User) -> str | None:
    email = ((user.prefs or {}).get("email") or "").strip()
    if email:
        return email
    raw = AESGCMVault(session).get(secret_name("personal", user.id))
    if raw is None:
        return None
    try:
        cfg = MailConfig.from_secret(raw, name=secret_name("personal", user.id))
    except (ValueError, KeyError):
        return None
    for candidate in (cfg.sender, cfg.username):
        if "@" in candidate:
            return candidate
    return None


def email_admins(session: Session, subject: str, body: str) -> int:
    """Email every admin from the notification mailbox (never from anyone's own mailbox).
    An admin with no address of their own gets it at the notification mailbox's address.
    Returns how many emails were sent: 0 when `mail/notify` isn't set up."""
    cfg = _config(AESGCMVault(session), NOTIFY_SECRET)
    if cfg is None:
        return 0
    admins = [u for u in session.query(User).all() if "admin" in (u.roles or []) and not u.disabled]
    to = {address_of(session, u) or cfg.sender for u in admins} or {cfg.sender}
    footer = "\n\n-- \nSent by your AI Platform server."
    for address in sorted(to):
        send(cfg, address, subject, body + footer)
    return len(to)


def email_person(session: Session, user: User, subject: str, body: str) -> bool:
    """False if there is no address or no mailbox to send from."""
    to = address_of(session, user)
    if not to:
        return False
    vault = AESGCMVault(session)
    cfg = _config(vault, NOTIFY_SECRET) or _config(vault, secret_name("personal", user.id))
    if cfg is None:
        return False
    footer = "\n\n-- \nSent by your AI Platform assistant."
    send(cfg, to, subject, body + footer)
    return True
