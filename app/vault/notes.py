"""Encryption for reminders and notifications.

These must be readable by the server when they fall due (a reminder at 9:00, the morning
mail digest), when the person isn't signed in, so their own chat key can't be used. They are
encrypted with a random "notes key" kept in the vault as `system/notes-key`, which is
wrapped by the vault master key like any other secret. Rotating the master key rewraps it, so
existing notes stay readable. The database alone (or a backup of it) can't open them.
"""
from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.orm import Session

from app.vault.aesgcm_vault import AESGCMVault

NOTES_KEY_NAME = "system/notes-key"
_NONCE = 12


def _key(session: Session) -> bytes:
    vault = AESGCMVault(session)
    raw = vault.get(NOTES_KEY_NAME)
    if raw is None:
        raw = base64.b64encode(os.urandom(32)).decode()
        vault.put(NOTES_KEY_NAME, raw)
    return base64.b64decode(raw)


def encrypt(session: Session, text: str, context: str) -> str:
    """`context` binds the ciphertext to its row (e.g. "reminder:<id>")."""
    nonce = os.urandom(_NONCE)
    blob = nonce + AESGCM(_key(session)).encrypt(nonce, text.encode(), context.encode())
    return base64.b64encode(blob).decode()


def decrypt(session: Session, blob: str, context: str) -> str:
    raw = base64.b64decode(blob)
    return AESGCM(_key(session)).decrypt(raw[:_NONCE], raw[_NONCE:], context.encode()).decode()
