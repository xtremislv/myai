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


def issue_token(user_id: str, username: str, roles: list[str], chat_key: bytes | None = None) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": user_id,
        "username": username,
        "roles": roles,
        "iat": now,
        "exp": now + dt.timedelta(seconds=_settings.jwt_ttl_seconds),
        "jti": uuid.uuid4().hex,
    }
    if chat_key is not None:
        payload["ck"] = user_keys.seal_for_token(chat_key, user_id)
    return jwt.encode(payload, _settings.jwt_secret, algorithm=_settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    return jwt.decode(token, _settings.jwt_secret, algorithms=[_settings.jwt_algorithm])
