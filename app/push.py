"""Push "check in" messages to the Android app through Firebase Cloud Messaging.

Nothing private goes through Google: a push carries only what kind of thing happened and, for a
chat, the app's own thread id. The app wakes up, signs in with its refresh token and fetches the
reminder, notification, reply or approval from this server itself.

Set up: put the Firebase service-account JSON in the vault as `push/fcm` (Admin > Secrets, or
PUT /v1/admin/secrets {"name": "push/fcm", "value": "<the JSON>"}). Without it, pushes are skipped
and the app falls back to checking every 15 minutes. Sending is direct (not OUTBOUND_PROXY, which
is for the agents' web traffic); set PUSH_PROXY if this host can't reach fcm.googleapis.com.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

import httpx
import jwt
from sqlalchemy.orm import Session

from app import devices

SECRET = "push/fcm"
SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
TIMEOUT = 10.0

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_access: dict[str, tuple[str, float]] = {}  # client_email -> (access token, expires at)


def _config(session: Session) -> dict | None:
    from app.vault.aesgcm_vault import AESGCMVault

    try:
        raw = AESGCMVault(session).get(SECRET)
    except Exception:  # noqa: BLE001 - no vault in some tests and tools
        return None
    if not raw:
        return None
    try:
        account = json.loads(raw)
    except ValueError:
        logger.warning("vault secret %s is not JSON: pushes are off", SECRET)
        return None
    if not all(account.get(k) for k in ("project_id", "client_email", "private_key")):
        logger.warning("vault secret %s lacks project_id/client_email/private_key: pushes are off", SECRET)
        return None
    return account


def configured(session: Session) -> bool:
    return _config(session) is not None


def _client() -> httpx.Client:
    proxy = os.environ.get("PUSH_PROXY") or None
    return httpx.Client(timeout=TIMEOUT, proxy=proxy)


def _access_token(account: dict, client: httpx.Client) -> str:
    email = account["client_email"]
    with _lock:
        cached = _access.get(email)
        if cached and cached[1] - 60 > time.time():
            return cached[0]
    now = int(time.time())
    assertion = jwt.encode(
        {"iss": email, "scope": SCOPE, "aud": TOKEN_URL, "iat": now, "exp": now + 3600},
        account["private_key"], algorithm="RS256",
    )
    response = client.post(TOKEN_URL, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion,
    })
    response.raise_for_status()
    body = response.json()
    with _lock:
        _access[email] = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
    return body["access_token"]


def _message(token: str, data: dict[str, str]) -> dict:
    # Data-only and high priority, so the app is woken even in Doze and builds its own notification.
    return {"message": {"token": token, "data": data, "android": {"priority": "high", "ttl": "86400s"}}}


def _send(account: dict, tokens: list[str], data: dict[str, str]) -> list[str]:
    """Send one message per token. Returns the tokens FCM says are gone."""
    gone = []
    with _client() as client:
        bearer = _access_token(account, client)
        url = SEND_URL.format(project=account["project_id"])
        for token in tokens:
            try:
                response = client.post(url, json=_message(token, data), headers={"authorization": f"Bearer {bearer}"})
            except httpx.HTTPError as e:
                logger.warning("push failed: %s", type(e).__name__)
                continue
            if response.status_code in (404, 410) or "UNREGISTERED" in response.text:
                gone.append(token)
            elif response.status_code >= 400:
                logger.warning("push refused: HTTP %s", response.status_code)
    return gone


def _deliver(account: dict, tokens: list[str], data: dict[str, str]) -> None:
    try:
        gone = _send(account, tokens, data)
    except Exception as e:  # noqa: BLE001 - a push is best effort; the app also checks by itself
        logger.warning("push failed: %s", type(e).__name__)
        return
    if gone:
        from app.db.session import SessionLocal

        with SessionLocal() as session:
            for token in gone:
                devices.forget_push_token(session, token)


# Tests replace this to see what would be sent.
def _dispatch(account: dict, tokens: list[str], data: dict[str, str]) -> None:
    threading.Thread(target=_deliver, args=(account, tokens, data), daemon=True, name="push").start()


def wake(session: Session, owner_ids: str | list[str], push_type: str, /, **extra: str) -> int:
    """Tell these people's phones that something of this kind is waiting. Never raises.
    Returns how many phones were sent a push."""
    try:
        ids = [owner_ids] if isinstance(owner_ids, str) else list(owner_ids)
        tokens = [t for owner in ids for t in devices.push_tokens(session, owner)]
        if not tokens:
            return 0
        account = _config(session)
        if account is None:
            return 0
        data = {"type": push_type, **{k: str(v)[:200] for k, v in extra.items() if v is not None}}
        _dispatch(account, tokens, data)
        return len(tokens)
    except Exception as e:  # noqa: BLE001 - never let a push break what triggered it
        logger.warning("push skipped: %s", type(e).__name__)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0


def wake_admins(session: Session, push_type: str, /, **extra: str) -> int:
    from app.db.session import User

    try:
        admins = [u.id for u in session.query(User).all() if "admin" in (u.roles or []) and not u.disabled]
    except Exception:  # noqa: BLE001
        return 0
    return wake(session, admins, push_type, **extra)
