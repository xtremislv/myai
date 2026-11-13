"""Password hashing + JWT issue/verify."""
from __future__ import annotations

import datetime as dt
import uuid

import bcrypt
import jwt

from app.config import get_settings
from app.vault import user_keys

_settings = get_settings()

# bcrypt only uses the first 72 bytes of a password, and the library raises on longer input.
_BCRYPT_MAX_BYTES = 72


def hash_password(raw: str) -> str:
    return bcrypt.hashpw(raw.encode()[:_BCRYPT_MAX_BYTES], bcrypt.gensalt()).decode()


def verify_password(raw: str, hashed: str) -> bool:
    return bcrypt.checkpw(raw.encode()[:_BCRYPT_MAX_BYTES], hashed.encode())


def issue_token(user_id: str, username: str, roles: list[str], chat_key: bytes | None = None, *,
                version: int = 0, auth_time: int | None = None) -> str:
    """`version` is the user's session_version (see check_session). `auth_time` is when the password
    was entered: kept unchanged when a session renews its token, so a session can't live forever."""
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": user_id,
        "username": username,
        "roles": roles,
        "iat": now,
        "exp": now + dt.timedelta(seconds=_settings.jwt_ttl_seconds),
        "jti": uuid.uuid4().hex,
        "sv": int(version or 0),
        "at": int(auth_time if auth_time is not None else now.timestamp()),
    }
    if chat_key is not None:
        payload["ck"] = user_keys.seal_for_token(chat_key, user_id)
    return jwt.encode(payload, _settings.jwt_secret, algorithm=_settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    return jwt.decode(token, _settings.jwt_secret, algorithms=[_settings.jwt_algorithm])


def token_for(user, chat_key: bytes | None = None, auth_time: int | None = None) -> str:
    return issue_token(user.id, user.username, user.roles, chat_key=chat_key,
                       version=user.session_version or 0, auth_time=auth_time)


def session_current(user, claims: dict) -> bool:
    """False once the person's sessions were ended (password change, reset, sign-out) after this
    token was issued. Tokens from before versions existed count as version 0."""
    return int(claims.get("sv", 0)) == int(user.session_version or 0)


def auth_time(claims: dict) -> int:
    return int(claims.get("at") or claims.get("iat") or 0)


def end_sessions(user) -> None:
    """Make every token issued to this person so far stop working (the caller commits)."""
    user.session_version = int(user.session_version or 0) + 1
