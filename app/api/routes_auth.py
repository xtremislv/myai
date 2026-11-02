"""Login + user registration (admin-gated)."""
from __future__ import annotations

import uuid

from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.acl.models import Subject
from app.api.deps import current_subject, require_admin
from app.auth.security import hash_password, issue_token, verify_password
from app.db.session import User, get_session
from app.vault import user_keys

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    # True until the user replaces the admin-set password; private chat needs this done.
    must_change_password: bool = False


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=12)


class CreateUserIn(BaseModel):
    username: str
    password: str
    roles: list[str] = []


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, session: Session = Depends(get_session)) -> TokenOut:
    user = session.query(User).filter(User.username == body.username).one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials")
    if user.wrapped_chat_key is None:
        return TokenOut(
            access_token=issue_token(user.id, user.username, user.roles), must_change_password=True
        )
    try:
        chat_key = user_keys.unlock(user, body.password)
    except InvalidTag as e:  # bcrypt only checks 72 bytes; the key needs the whole password
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials") from e
    return TokenOut(access_token=issue_token(user.id, user.username, user.roles, chat_key=chat_key))


@router.post("/change-password", response_model=TokenOut)
def change_password(
    body: ChangePasswordIn,
    subject: Subject = Depends(current_subject),
    session: Session = Depends(get_session),
) -> TokenOut:
    user = session.get(User, subject.id)
    if user is None or not verify_password(body.old_password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials")
    if body.new_password == body.old_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "new password must be different")
    try:
        if user.wrapped_chat_key is None:
            chat_key = user_keys.provision(user, body.new_password)
        else:
            chat_key = user_keys.rewrap(user, body.old_password, body.new_password)
    except InvalidTag as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials") from e
    user.password_hash = hash_password(body.new_password)
    session.commit()
    return TokenOut(access_token=issue_token(user.id, user.username, user.roles, chat_key=chat_key))


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(
    body: CreateUserIn,
    session: Session = Depends(get_session),
    _admin=Depends(require_admin),
) -> dict:
    if session.query(User).filter(User.username == body.username).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "username exists")
    user = User(
        id=uuid.uuid4().hex,
        username=body.username,
        password_hash=hash_password(body.password),
        roles=body.roles,
    )
    session.add(user)
    session.commit()
    return {"id": user.id, "username": user.username, "roles": user.roles}
