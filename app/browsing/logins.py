"""Saved website logins (cookies and local storage) for each person's browser.

A person logs in to a shop once, in the live browser view; the session is saved so the agent
(and the person) find it logged in next time, even after the browser closes.

* Private browsing ("private" scope): encrypted with the person's own chat key, stored in MinIO
  at browserstate/<user id>. Nobody else, the admin included, can use it.
* Work browsing (a department scope): in the vault as browser/<department>/<user id>, so the
  department's agent can resume work without the person's key. The admin is denied these
  secrets (ACL) and can't see them on the Credentials page.
"""
from __future__ import annotations

import json
import logging
import time

from app.vault import user_keys

logger = logging.getLogger(__name__)

SAVE_EVERY = 20.0  # seconds between automatic saves of the same session
_last_saved: dict[str, float] = {}


def session_key(subject_id: str, scope: str) -> str:
    return f"{subject_id}:{scope}"


def _object(subject_id: str) -> str:
    return f"browserstate/{subject_id}"


def _secret(subject_id: str, scope: str) -> str:
    return f"browser/{scope}/{subject_id}"


def load(subject_id: str, scope: str, chat_key: bytes | None) -> dict | None:
    try:
        if scope == "private":
            if chat_key is None:
                return None
            from app.storage.objects import get_system_object, system_object_size

            if system_object_size(_object(subject_id)) is None:
                return None
            raw = user_keys.decrypt_bytes(chat_key, get_system_object(_object(subject_id)), f"browserstate:{subject_id}")
            return json.loads(raw)
        from app.db.session import SessionLocal
        from app.vault.aesgcm_vault import AESGCMVault

        with SessionLocal() as session:
            raw = AESGCMVault(session).get(_secret(subject_id, scope))
        return json.loads(raw) if raw else None
    except Exception as e:  # noqa: BLE001 - a lost login just means logging in again
        logger.warning("could not load a saved browser login: %s", type(e).__name__)
        return None


def save(subject_id: str, scope: str, state: dict | None, chat_key: bytes | None, *, force: bool = False) -> None:
    key = session_key(subject_id, scope)
    if state is None or (not force and time.monotonic() - _last_saved.get(key, 0) < SAVE_EVERY):
        return
    try:
        data = json.dumps(state)
        if scope == "private":
            if chat_key is None:
                return
            from app.storage.objects import put_system_object

            put_system_object(_object(subject_id), user_keys.encrypt_bytes(chat_key, data.encode(), f"browserstate:{subject_id}"))
        else:
            from app.db.session import SessionLocal
            from app.vault.aesgcm_vault import AESGCMVault

            with SessionLocal() as session:
                AESGCMVault(session).put(_secret(subject_id, scope), data)
        _last_saved[key] = time.monotonic()
    except Exception as e:  # noqa: BLE001
        logger.warning("could not save a browser login: %s", type(e).__name__)


def forget(subject_id: str, scope: str) -> None:
    """Log out everywhere: drop the saved session (the open browser is closed by the caller)."""
    if scope == "private":
        from app.storage.objects import remove_expired_object

        try:
            remove_expired_object(_object(subject_id))
        except Exception:  # noqa: BLE001
            pass
    else:
        from app.db.session import SessionLocal
        from app.vault.aesgcm_vault import AESGCMVault

        with SessionLocal() as session:
            AESGCMVault(session).delete(_secret(subject_id, scope))
    _last_saved.pop(session_key(subject_id, scope), None)


def erase_owner(session, subject_id: str, remove_object) -> None:
    from app.vault.aesgcm_vault import AESGCMVault

    try:
        remove_object(_object(subject_id))
    except Exception:  # noqa: BLE001
        pass
    vault = AESGCMVault(session)
    for name in vault.list_names():
        if name.startswith("browser/") and name.endswith(f"/{subject_id}"):
            vault.delete(name)


def open_session(subject_id: str, scope: str, chat_key: bytes | None) -> str:
    """Make sure this person's browser for `scope` is open, with their saved logins. Returns its key."""
    from app.browsing.browser import browser

    key = session_key(subject_id, scope)
    b = browser()
    if not b.has(key):
        b.call(b.ensure, key, load(subject_id, scope, chat_key))
    return key


def persist(subject_id: str, scope: str, chat_key: bytes | None, *, force: bool = False) -> None:
    from app.browsing.browser import browser

    key = session_key(subject_id, scope)
    b = browser()
    if b.has(key):
        save(subject_id, scope, b.call(b.state_of, key), chat_key, force=force)
