"""Emailing a person their reminders and digests.

The address is the one in their settings, or else their connected mailbox's own address.
It is sent from the company's notification mailbox: vault secret `mail/notify` if there is one,
else the admin department's official mailbox `mail/contact`. Reminders fall back to the person's
own mailbox, sending to itself. Welcome emails go to each new address once (`welcome_new_addresses`).
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.db.session import User
from app.mail.connector import MailConfig, secret_name, send
from app.vault.aesgcm_vault import AESGCMVault

NOTIFY_SECRET = "mail/notify"
OFFICIAL_SECRET = "mail/contact"  # the admin department's official mailbox

logger = logging.getLogger(__name__)


def _config(vault: AESGCMVault, name: str) -> MailConfig | None:
    raw = vault.get(name)
    if raw is None:
        return None
    try:
        cfg = MailConfig.from_secret(raw, guarded=name.startswith("mail/personal/"), name=name)
    except (ValueError, KeyError):
        return None
    return cfg if cfg.smtp_host else None


def _sender(vault: AESGCMVault) -> MailConfig | None:
    return _config(vault, NOTIFY_SECRET) or _config(vault, OFFICIAL_SECRET)


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
    Returns how many emails were sent: 0 when there is no notification mailbox."""
    cfg = _sender(AESGCMVault(session))
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
    cfg = _sender(vault) or _config(vault, secret_name("personal", user.id))
    if cfg is None:
        return False
    footer = "\n\n-- \nSent by your AI Platform assistant."
    send(cfg, to, subject, body + footer)
    return True


WELCOME_SUBJECT = "Welcome to the AI Platform"


def welcome_text(user: User) -> str:
    return (
        f"Hi {user.username},\n\n"
        "Welcome to the AI Platform! This address is now linked to your account, so this is where the\n"
        "platform will email you.\n\n"
        "You choose what arrives here, under My account in the web app:\n"
        "  - reminders: tick \"Email me every reminder, too\" (or ask for an email when you set one);\n"
        "  - a morning summary of your new mail: tick \"Every morning, summarize my new mail...\", pick\n"
        "    the hour, and tick \"Also email me the digest\";\n"
        + ("  - server alerts: as an admin you get these automatically.\n" if "admin" in (user.roles or []) else "")
        + "\nAsk your admin for the current link to the app if you don't have it.\n"
    )


def welcome_new_addresses(session: Session) -> int:
    """Send a welcome email, once, to every address a person has added or linked (their email
    setting or connected mailbox). Only from the notification mailbox, never from their own.
    A failed send is tried again next time. Returns how many were sent."""
    cfg = _sender(AESGCMVault(session))
    if cfg is None:
        return 0
    sent = 0
    for user in session.query(User).filter(User.disabled.is_(False)).all():
        address = address_of(session, user)
        welcomed = list((user.prefs or {}).get("welcomed") or [])
        if not address or address.lower() in welcomed:
            continue
        try:
            send(cfg, address, WELCOME_SUBJECT, welcome_text(user) + "\n-- \nSent by your AI Platform server.")
        except (OSError, ValueError):  # a bad address mustn't hold up everyone else's
            logger.warning("welcome email to user %s failed; trying again later", user.id, exc_info=True)
            continue
        user.prefs = {**(user.prefs or {}), "welcomed": [*welcomed, address.lower()]}
        session.commit()
        sent += 1
    return sent
