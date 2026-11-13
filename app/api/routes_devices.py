"""The phone app's sign-in: devices with refresh tokens, push registration and app config.

The browser keeps its session in an HttpOnly cookie; the Android app keeps a refresh token in
the phone's keystore instead (app/devices.py). A typical app session:

  POST /v1/auth/device/login    {username, password, name}        -> access + refresh token
  POST /v1/auth/device/refresh  {device_id, refresh_token}        -> new access + refresh token
  PUT  /v1/devices/{id}/push    {push_token}                      -> FCM token for wake-ups
  DELETE /v1/devices/{id}                                         -> sign this phone out
"""
from __future__ import annotations

import collections
import datetime as dt
import logging
import threading
import time
from typing import Literal

from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import app_release, devices, push
from app.acl.models import Subject
from app.api.deps import checked_claims, current_subject
from app.auth.security import token_for, verify_password
from app.config import get_settings
from app.db.session import User, get_session
from app.vault import user_keys

router = APIRouter(tags=["devices"])
logger = logging.getLogger(__name__)

API_VERSION = 1


class DeviceLoginIn(BaseModel):
    username: str
    password: str
    name: str = Field(default="", max_length=128)  # "Pixel 8", shown under My account > Devices
    platform: Literal["android"] = "android"
    push_token: str | None = Field(default=None, max_length=512)


class DeviceRegisterIn(BaseModel):
    name: str = Field(default="", max_length=128)
    platform: Literal["android"] = "android"
    push_token: str | None = Field(default=None, max_length=512)
    # Sign this same phone in again (e.g. after setting your own password, so it gets the chat key).
    device_id: str | None = Field(default=None, max_length=64)


class RefreshIn(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    refresh_token: str = Field(min_length=1, max_length=512)


class DeviceTokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: str
    device_id: str
    must_change_password: bool = False
    # Present only when a new recovery code was just created (never here today; kept for parity).
    recovery_code: str | None = None


class DeviceOut(BaseModel):
    id: str
    name: str
    platform: str
    push: bool
    created_at: dt.datetime
    last_used_at: dt.datetime | None = None


class PushIn(BaseModel):
    push_token: str | None = Field(default=None, max_length=512)


class AndroidReleaseOut(BaseModel):
    version_code: int
    version_name: str
    sha256: str
    size: int
    notes: str = ""
    min_version_code: int = 0
    published_at: str = ""
    url: str


class AppConfigOut(BaseModel):
    api_version: int
    push_enabled: bool
    token_ttl_seconds: int
    poll_minutes: int = 15
    attachment_max_bytes: int
    android: AndroidReleaseOut | None = None  # the app this server offers; None until one is published


def _token_out(user: User, chat_key: bytes | None, device_id: str, refresh_token: str) -> DeviceTokenOut:
    return DeviceTokenOut(
        access_token=token_for(user, chat_key),
        expires_in=get_settings().jwt_ttl_seconds,
        refresh_token=refresh_token,
        device_id=device_id,
        must_change_password=user.wrapped_chat_key is None,
    )


def _optional_chat_key(checked: tuple[dict, Subject] = Depends(checked_claims)) -> bytes | None:
    """The caller's chat key if their token carries one (it doesn't until they set their own password)."""
    claims = checked[0]
    if not claims.get("ck"):
        return None
    try:
        return user_keys.open_from_token(claims["ck"], claims["sub"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid chat key; log in again") from e


@router.post("/v1/auth/device/login", response_model=DeviceTokenOut)
def device_login(body: DeviceLoginIn, session: Session = Depends(get_session)) -> DeviceTokenOut:
    """Sign a phone in with username and password. Same checks as /v1/auth/login."""
    user = session.query(User).filter(User.username == body.username).one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials")
    if user.disabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this account is disabled")
    chat_key = None
    if user.wrapped_chat_key is not None:
        try:
            chat_key = user_keys.unlock(user, body.password)
        except InvalidTag as e:  # bcrypt only checks 72 bytes; the key needs the whole password
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials") from e
    row, refresh_token = devices.register(session, user, chat_key, name=body.name, platform=body.platform,
                                          push_token=body.push_token)
    return _token_out(user, chat_key, row.id, refresh_token)


@router.post("/v1/auth/device/refresh", response_model=DeviceTokenOut)
def device_refresh(body: RefreshIn, session: Session = Depends(get_session)) -> DeviceTokenOut:
    """A fresh access token for a signed-in phone. The refresh token is replaced: keep the new one."""
    try:
        user, row, chat_key, refresh_token = devices.refresh(session, body.device_id, body.refresh_token)
    except devices.DeviceError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e
    return _token_out(user, chat_key, row.id, refresh_token)


@router.post("/v1/devices", response_model=DeviceTokenOut, status_code=status.HTTP_201_CREATED)
def register_device(
    body: DeviceRegisterIn,
    subject: Subject = Depends(current_subject),
    chat_key: bytes | None = Depends(_optional_chat_key),
    session: Session = Depends(get_session),
) -> DeviceTokenOut:
    """Turn a signed-in session (e.g. right after /v1/auth/change-password) into a device sign-in,
    or with device_id, sign the same phone in again so it holds the current chat key."""
    user = session.get(User, subject.id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    try:
        row, refresh_token = devices.register(session, user, chat_key, name=body.name, platform=body.platform,
                                              push_token=body.push_token, replace_id=body.device_id)
    except devices.DeviceError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    return _token_out(user, chat_key, row.id, refresh_token)


@router.get("/v1/devices", response_model=list[DeviceOut])
def list_devices(subject: Subject = Depends(current_subject), session: Session = Depends(get_session)) -> list[DeviceOut]:
    return [
        DeviceOut(id=r.id, name=r.name, platform=r.platform, push=bool(r.push_token), created_at=r.created_at,
                  last_used_at=r.last_used_at)
        for r in devices.list_for(session, subject.id)
    ]


@router.delete("/v1/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def sign_out_device(
    device_id: str, subject: Subject = Depends(current_subject), session: Session = Depends(get_session),
) -> None:
    if not devices.revoke(session, subject.id, device_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such device")


@router.put("/v1/devices/{device_id}/push", response_model=DeviceOut)
def set_push(
    device_id: str, body: PushIn, subject: Subject = Depends(current_subject),
    session: Session = Depends(get_session),
) -> DeviceOut:
    """Register (or clear, with null) this phone's Firebase token for wake-up pushes."""
    if not devices.set_push_token(session, subject.id, device_id, body.push_token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such device")
    r = next(d for d in devices.list_for(session, subject.id) if d.id == device_id)
    return DeviceOut(id=r.id, name=r.name, platform=r.platform, push=bool(r.push_token), created_at=r.created_at,
                     last_used_at=r.last_used_at)


@router.get("/v1/app/config", response_model=AppConfigOut)
def app_config(session: Session = Depends(get_session)) -> AppConfigOut:
    """What the phone app needs to know before signing in. Nothing about any person."""
    settings = get_settings()
    return AppConfigOut(
        api_version=API_VERSION, push_enabled=push.configured(session),
        token_ttl_seconds=settings.jwt_ttl_seconds, attachment_max_bytes=settings.attachment_max_bytes,
        android=app_release.latest(),
    )


@router.get("/v1/app/android/latest.apk", include_in_schema=False)
def android_latest() -> RedirectResponse:
    """A stable link to the newest app; redirects to its versioned file."""
    release = app_release.latest()
    if release is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The Android app hasn't been published on this server yet.")
    return RedirectResponse(release["url"], status_code=status.HTTP_302_FOUND,
                            headers={"Cache-Control": "no-store"})


@router.get("/v1/app/android/{version_code}.apk", include_in_schema=False)
def android_apk(version_code: int) -> FileResponse:
    """The published APK. Only the current version is served; the URL changes with each release,
    so caches (Cloudflare, the phone) can never hand out an old app under a new name."""
    release = app_release.latest()
    if release is None or release["version_code"] != version_code:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That version of the app isn't available. Get the latest one.")
    return FileResponse(
        app_release.apk_path(), media_type="application/vnd.android.package-archive",
        filename=f"TW-ai-{release['version_name']}.apk",
        headers={"Cache-Control": "public, max-age=86400, immutable", "X-Content-Type-Options": "nosniff"},
    )


# ---- crash reports from the Android app ----
class CrashIn(BaseModel):
    version_code: int = 0
    version_name: str = Field(default="", max_length=32)
    android: int = 0
    device: str = Field(default="", max_length=128)
    trace: str = Field(min_length=1, max_length=16000)


_crash_lock = threading.Lock()
_crash_times: collections.deque[float] = collections.deque(maxlen=30)


@router.post("/v1/app/crash", status_code=status.HTTP_204_NO_CONTENT, include_in_schema=False)
def app_crash(body: CrashIn) -> None:
    """A stack trace from a phone that crashed, sent on its next start, written to the gateway log
    for the developer. No sign-in (the app may crash before it), so at most 30 a minute are kept;
    the app sends only the exception and stack frames, never chat or mail text."""
    now = time.monotonic()
    with _crash_lock:
        if len(_crash_times) == _crash_times.maxlen and now - _crash_times[0] < 60:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many crash reports; try later.")
        _crash_times.append(now)
    trace = "".join(ch if ch.isprintable() or ch in "\n\t" else "?" for ch in body.trace)
    logger.warning("ANDROID CRASH app=%s (%s) android=%s device=%r\n%s",
                   body.version_name, body.version_code, body.android, body.device, trace)
