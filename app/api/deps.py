"""Shared FastAPI dependencies: current subject from JWT, admin guard."""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.acl.engine import ACLEngine
from app.acl.models import Action, Resource, Subject
from app.auth.security import decode_token
from app.db.session import get_session
from app.vault import user_keys

_bearer = HTTPBearer(auto_error=True)


def current_subject(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> Subject:
    try:
        claims = decode_token(creds.credentials)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {e}") from e
    return Subject(id=claims["sub"], roles=tuple(claims.get("roles", [])))


def chat_key(creds: HTTPAuthorizationCredentials = Depends(_bearer)) -> bytes:
    """The caller's private-chat key, carried sealed inside their token."""
    try:
        claims = decode_token(creds.credentials)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {e}") from e
    sealed = claims.get("ck")
    if sealed is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "change your password to enable private chat"
        )
    try:
        return user_keys.open_from_token(sealed, claims["sub"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid chat key; log in again") from e


def require_admin(
    subject: Subject = Depends(current_subject),
    session: Session = Depends(get_session),
) -> Subject:
    decision = ACLEngine(session).check(subject, Action.ADMIN, Resource("system", "*"))
    if not decision.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, decision.reason)
    return subject
