"""Login + user registration (admin-gated)."""
from __future__ import annotations

import datetime as dt
import uuid

from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.acl.models import Subject
from app.api.deps import current_subject, require_admin
from app.auth.security import hash_password, issue_token, verify_password
from app.db.session import User, get_session
from app.departments.packs import DEFAULT_DEPARTMENTS
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
    # Only present when a new code was just created. It is never shown again.
    recovery_code: str | None = None


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=12)


class RecoverIn(BaseModel):
    username: str
    recovery_code: str
    new_password: str = Field(min_length=12)


class PasswordIn(BaseModel):
    password: str


class RecoveryCodeOut(BaseModel):
    recovery_code: str


class UserOut(BaseModel):
    id: str
    username: str
    roles: list[str]
    disabled: bool = False
    chat_enabled: bool


class ProfileOut(BaseModel):
    id: str
    username: str
    is_admin: bool
    departments: list[str]
    chat_enabled: bool
    created_at: dt.datetime


class DepartmentsIn(BaseModel):
    departments: list[str]


ADMIN_ROLE = "admin"
ASSIGNABLE = {d.name for d in DEFAULT_DEPARTMENTS} - {ADMIN_ROLE}


class CreateUserIn(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=12)
    roles: list[str] = []


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, session: Session = Depends(get_session)) -> TokenOut:
    user = session.query(User).filter(User.username == body.username).one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials")
    if user.disabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this account is disabled")
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
    recovery_code = None
    try:
        if user.wrapped_chat_key is None:
            chat_key = user_keys.provision(user, body.new_password)
            recovery_code = user_keys.add_recovery_code(user, chat_key)
        else:
            chat_key = user_keys.rewrap(user, body.old_password, body.new_password)
    except InvalidTag as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials") from e
    user.password_hash = hash_password(body.new_password)
    session.commit()
    return TokenOut(
        access_token=issue_token(user.id, user.username, user.roles, chat_key=chat_key),
        recovery_code=recovery_code,
    )


@router.post("/recover", response_model=TokenOut)
def recover(body: RecoverIn, session: Session = Depends(get_session)) -> TokenOut:
    """Set a new password with a recovery code. The code is used up: a new one is issued."""
    user = session.query(User).filter(User.username == body.username).one_or_none()
    if user is None or user.wrapped_recovery_key is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad username or recovery code")
    if user.disabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this account is disabled")
    try:
        chat_key = user_keys.unlock_with_recovery_code(user, body.recovery_code)
    except InvalidTag as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad username or recovery code") from e
    user_keys.wrap_with_password(user, body.new_password, chat_key)
    new_code = user_keys.add_recovery_code(user, chat_key)
    user.password_hash = hash_password(body.new_password)
    session.commit()
    return TokenOut(
        access_token=issue_token(user.id, user.username, user.roles, chat_key=chat_key),
        recovery_code=new_code,
    )


@router.post("/recovery-code", response_model=RecoveryCodeOut)
def new_recovery_code(
    body: PasswordIn,
    subject: Subject = Depends(current_subject),
    session: Session = Depends(get_session),
) -> RecoveryCodeOut:
    """Replace the recovery code, for a user who lost it but still has their password."""
    user = session.get(User, subject.id)
    if user is None or user.wrapped_chat_key is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials")
    try:
        chat_key = user_keys.unlock(user, body.password)
    except InvalidTag as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials") from e
    code = user_keys.add_recovery_code(user, chat_key)
    session.commit()
    return RecoveryCodeOut(recovery_code=code)


@router.get("/me", response_model=ProfileOut)
def my_profile(
    subject: Subject = Depends(current_subject),
    session: Session = Depends(get_session),
) -> ProfileOut:
    user = session.get(User, subject.id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    return ProfileOut(
        id=user.id,
        username=user.username,
        is_admin=ADMIN_ROLE in user.roles,
        departments=sorted(set(user.roles) - {ADMIN_ROLE}),
        chat_enabled=user.wrapped_chat_key is not None,
        created_at=user.created_at,
    )


@router.put("/users/{user_id}/departments", response_model=UserOut)
def set_departments(
    user_id: str,
    body: DepartmentsIn,
    session: Session = Depends(get_session),
    _admin=Depends(require_admin),
) -> UserOut:
    """Replace a user's departments, effective immediately. A profile can hold any number
    of them. This does not change who is admin."""
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    unknown = set(body.departments) - ASSIGNABLE
    if unknown:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown departments: {sorted(unknown)}")
    keep_admin = [ADMIN_ROLE] if ADMIN_ROLE in user.roles else []
    user.roles = keep_admin + sorted(set(body.departments))
    session.commit()
    return _user_out(user)


@router.get("/users", response_model=list[UserOut])
def list_users(session: Session = Depends(get_session), _admin=Depends(require_admin)) -> list[UserOut]:
    return [_user_out(u) for u in session.query(User).order_by(User.username).all()]


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


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id, username=user.username, roles=user.roles,
        chat_enabled=user.wrapped_chat_key is not None, disabled=bool(user.disabled),
    )


def _other_user(session: Session, user_id: str, admin: Subject) -> User:
    """The user an admin action targets. Admins manage their own account under My account,
    and can't lock themselves out: there is only one admin."""
    if user_id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "use My account to change your own account")
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    return user


class DisabledIn(BaseModel):
    disabled: bool


@router.put("/users/{user_id}/disabled", response_model=UserOut)
def set_disabled(
    user_id: str,
    body: DisabledIn,
    session: Session = Depends(get_session),
    admin: Subject = Depends(require_admin),
) -> UserOut:
    """Disable or re-enable an account. Takes effect on the user's next request, because
    every request reads the account from the database. Nothing is deleted."""
    user = _other_user(session, user_id, admin)
    user.disabled = body.disabled
    session.commit()
    return _user_out(user)


class ResetPasswordIn(BaseModel):
    password: str = Field(min_length=12)


@router.post("/users/{user_id}/reset-password", response_model=UserOut)
def reset_password(
    user_id: str,
    body: ResetPasswordIn,
    session: Session = Depends(get_session),
    admin: Subject = Depends(require_admin),
) -> UserOut:
    """Give a user a temporary password, for someone who lost theirs and their recovery code.

    The admin never gets a key to the user's private chats. The password's copy of the chat
    key is removed and the recovery code's copy is kept, so:
      - with their recovery code, "Forgot password?" still restores every private chat;
      - signing in with the temporary password makes them choose a new one and starts a new
        chat key: earlier private chats can no longer be opened (they stop being listed).
    """
    user = _other_user(session, user_id, admin)
    user.password_hash = hash_password(body.password)
    user.chat_key_salt = user.chat_key_nonce = user.wrapped_chat_key = None
    session.commit()
    return _user_out(user)


class EraseIn(BaseModel):
    delete_account: bool = False
    confirm_username: str  # typed by the admin, so a slip can't erase the wrong person


@router.post("/users/{user_id}/erase")
def erase_user_data(
    user_id: str,
    body: EraseIn,
    session: Session = Depends(get_session),
    admin: Subject = Depends(require_admin),
) -> dict:
    """Delete everything stored for a person, on their request (see app/privacy.py). This
    cannot be undone. NSFW reports and the audit log are kept."""
    from app.privacy import erase_user

    user = _other_user(session, user_id, admin)
    if body.confirm_username != user.username:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "type the user's exact username to confirm")
    return erase_user(session, user_id, delete_account=body.delete_account)
