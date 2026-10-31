"""Login + user registration (admin-gated)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.auth.security import hash_password, issue_token, verify_password
from app.db.session import User, get_session

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class CreateUserIn(BaseModel):
    username: str
    password: str
    roles: list[str] = []


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, session: Session = Depends(get_session)) -> TokenOut:
    user = session.query(User).filter(User.username == body.username).one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials")
    return TokenOut(access_token=issue_token(user.id, user.username, user.roles))


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
