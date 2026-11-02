"""Shared FastAPI dependencies: current subject from JWT, admin guard."""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.acl.engine import ACLEngine
from app.acl.models import Action, Resource, Subject
from app.auth.security import decode_token
from app.db.session import User, get_session, release_connection
from app.vault import user_keys

_bearer = HTTPBearer(auto_error=True)


def subject_from_claims(session: Session, claims: dict) -> Subject:
    """Who the token belongs to, with their departments as they are right now. The roles
    inside a token are ignored, so removing someone from a department or deleting them
    takes effect immediately rather than when their token expires."""
    user = session.get(User, claims["sub"])
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "this account no longer exists")
    if user.disabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this account is disabled")
    return Subject(id=user.id, roles=tuple(user.roles))


def current_subject(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
    session: Session = Depends(get_session),
) -> Subject:
    try:
        claims = decode_token(creds.credentials)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {e}") from e
    subject = subject_from_claims(session, claims)
    # Routes may call the model next, so don't leave this lookup's transaction open.
    release_connection(session)
    return subject


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
