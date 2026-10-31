"""Password hashing + JWT issue/verify."""
from __future__ import annotations

import datetime as dt
import uuid

import jwt
from passlib.context import CryptContext

from app.config import get_settings

_settings = get_settings()
_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(raw: str) -> str:
    return _pwd.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    return _pwd.verify(raw, hashed)


def issue_token(user_id: str, username: str, roles: list[str]) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": user_id,
        "username": username,
        "roles": roles,
        "iat": now,
        "exp": now + dt.timedelta(seconds=_settings.jwt_ttl_seconds),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, _settings.jwt_secret, algorithm=_settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    return jwt.decode(token, _settings.jwt_secret, algorithms=[_settings.jwt_algorithm])
