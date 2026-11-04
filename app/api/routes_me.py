"""A person's own things: settings, mailbox connection, notifications (the bell), reminders
and files ("My files"). Everything here acts on the caller only."""
from __future__ import annotations

import datetime as dt
import json
from typing import Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Form, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.acl.models import Subject
from app.api.deps import chat_key, current_subject
from app.config import get_settings
from app.db.session import User, get_session, release_connection
from app.mail import connector
from app.reminders import service
from app.storage import userfiles
from app.vault.aesgcm_vault import AESGCMVault

router = APIRouter(prefix="/v1", tags=["me"])

NSFW_SAMPLE_CHARS = 4000


def _user(session: Session, subject: Subject) -> User:
    user = session.get(User, subject.id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "this account has no settings")
    return user


# ---- settings ----
class PrefsIO(BaseModel):
    timezone: str = ""
    email: str = Field(default="", max_length=254)
    digest_hour: int | None = Field(default=None, ge=0, le=23)  # None = no daily digest
    digest_email: bool = False
    email_reminders: bool = False


@router.get("/me/prefs", response_model=PrefsIO)
def get_prefs(subject: Subject = Depends(current_subject), session: Session = Depends(get_session)) -> PrefsIO:
    prefs = dict(_user(session, subject).prefs or {})
    prefs.setdefault("timezone", get_settings().timezone)
    return PrefsIO(**{k: v for k, v in prefs.items() if k in PrefsIO.model_fields})


@router.put("/me/prefs", response_model=PrefsIO)
def put_prefs(body: PrefsIO, subject: Subject = Depends(current_subject), session: Session = Depends(get_session)) -> PrefsIO:
    if body.timezone:
        try:
            ZoneInfo(body.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown timezone '{body.timezone}'") from None
    if body.email:
        try:
            connector.valid_recipients(body.email)
        except ValueError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
    user = _user(session, subject)
    user.prefs = {**(user.prefs or {}), **body.model_dump()}
    session.commit()
    return body


# ---- personal mailbox ----
class MailboxIn(BaseModel):
    """Usually just the address and password: the servers are chosen from the address (Gmail
    first). imap_host etc. are only for providers not in app/mail/providers.py."""

    address: str = Field(default="", max_length=255)
    password: str = Field(min_length=1, max_length=1024)
    imap_host: str = Field(default="", max_length=255)
    imap_port: int = 993
    smtp_host: str = Field(default="", max_length=255)
    smtp_port: int = 465
    username: str = Field(default="", max_length=255)  # defaults to the address
    sender: str = Field(default="", max_length=255, alias="from")
    check: bool = True  # try logging in before saving

    model_config = {"populate_by_name": True}


class MailboxOut(BaseModel):
    connected: bool
    imap_host: str = ""
    smtp_host: str = ""
    username: str = ""
    method: str = ""  # "password", "google sign-in" or "microsoft sign-in"
    # Which "Sign in with ..." buttons work on this server (the admin added the app's client).
    sign_in: dict[str, bool] = Field(default_factory=dict)


def _secret(subject: Subject) -> str:
    return connector.secret_name("personal", subject.id)


def _out(cfg: connector.MailConfig) -> MailboxOut:
    from app.mail import oauth

    return MailboxOut(connected=True, imap_host=cfg.imap_host, smtp_host=cfg.smtp_host, username=cfg.username,
                      method=cfg.method, sign_in=oauth.configured())


@router.get("/me/mailbox", response_model=MailboxOut)
def get_mailbox(subject: Subject = Depends(current_subject), session: Session = Depends(get_session)) -> MailboxOut:
    from app.mail import oauth

    raw = AESGCMVault(session).get(_secret(subject))
    if raw is None:
        return MailboxOut(connected=False, sign_in=oauth.configured())
    try:
        cfg = connector.MailConfig.from_secret(raw)
    except (ValueError, KeyError):
        return MailboxOut(connected=False, sign_in=oauth.configured())
    return _out(cfg)


def _config_for(body: MailboxIn, imap: str, smtp: str, smtp_port: int):
    user = (body.username or body.address).strip()
    raw = json.dumps({
        "imap_host": imap, "imap_port": body.imap_port, "smtp_host": smtp, "smtp_port": smtp_port,
        "username": user, "password": "".join(body.password.split()) if "gmail" in imap else body.password.strip(),
        "from": body.sender.strip() or body.address.strip() or user,
    })
    return raw, connector.MailConfig.from_secret(raw, guarded=True)


def _refused(provider, error: Exception) -> str:
    import smtplib

    from app.mail import providers

    said = " ".join((str(error) or type(error).__name__).strip("b'\"").split())
    if isinstance(error, OSError) and not isinstance(error, smtplib.SMTPException):
        return f"couldn't reach {provider.imap_host if provider else 'the mail server'} ({type(error).__name__}); try again"
    return providers.explain(provider, said) if provider else f"the mail server refused the sign-in ({said[:160]})"


@router.put("/me/mailbox", response_model=MailboxOut)
def put_mailbox(body: MailboxIn, subject: Subject = Depends(current_subject), session: Session = Depends(get_session)) -> MailboxOut:
    from app.mail import providers

    address = (body.address or body.username).strip()
    if "@" not in address and not body.imap_host:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "enter your email address")
    if body.imap_host:  # a provider we don't know: the person gave the servers
        tries = [(None, body.imap_host.strip(), body.smtp_host.strip(), body.smtp_port)]
    else:
        tries = [(p, p.imap_host, p.smtp_host, p.smtp_port) for p in providers.candidates(address)]
    problems = []
    release_connection(session)
    for provider, imap, smtp, smtp_port in tries:
        if provider is not None and not provider.password_ok:
            problems.append(provider.help)
            continue
        raw, cfg = _config_for(body, imap, smtp, smtp_port)
        try:
            connector.check_servers(cfg)
        except ValueError as e:
            problems.append(str(e))
            continue
        if body.check:
            try:
                connector.check_login(cfg)
            except Exception as e:  # noqa: BLE001 - the provider's own words help the person fix it
                problems.append(_refused(provider, e))
                continue
        AESGCMVault(session).put(_secret(subject), raw)
        return _out(cfg)
    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, " ".join(dict.fromkeys(problems)) or "couldn't connect")


# ---- signing in with Google or Microsoft (OAuth, app/mail/oauth.py) ----
class GoogleStartIn(BaseModel):
    address: str = Field(default="", max_length=255)


class GoogleStartOut(BaseModel):
    url: str
    paste: bool  # the person pastes the address the browser ends on back here


class GoogleFinishIn(BaseModel):
    url: str = Field(min_length=1, max_length=4000)  # the address the browser ended on (or the code)


class MicrosoftStartOut(BaseModel):
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int


class MicrosoftPollOut(BaseModel):
    done: bool
    mailbox: MailboxOut | None = None


def _save_signed_in(session: Session, subject_id: str, data: dict) -> MailboxOut:
    """Check the new tokens actually open the mailbox, then keep them."""
    name = connector.secret_name("personal", subject_id)
    raw = json.dumps(data)
    cfg = connector.MailConfig.from_secret(raw, guarded=True, name=name)
    release_connection(session)
    try:
        connector.check_login(cfg)
    except Exception as e:  # noqa: BLE001
        said = " ".join((str(e) or type(e).__name__).strip("b'\"").split())[:200]
        hint = (" In Gmail, check that IMAP is on (Settings > Forwarding and POP/IMAP)."
                if data.get("provider") == "google" else
                " For a work or school account, your organisation may have to allow IMAP for it.")
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"Signed in, but the mailbox refused the connection ({said}).{hint}") from None
    AESGCMVault(session).put(name, raw)
    return _out(cfg)


def _oauth_call(fn, *args):
    from app.mail.oauth import OAuthError

    try:
        return fn(*args)
    except OAuthError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None


@router.post("/me/mailbox/oauth/google/start", response_model=GoogleStartOut)
def google_start(body: GoogleStartIn, subject: Subject = Depends(current_subject)) -> GoogleStartOut:
    from app.mail import oauth

    return GoogleStartOut(**_oauth_call(oauth.google_start, subject.id, body.address.strip()))


@router.post("/me/mailbox/oauth/google/finish", response_model=MailboxOut)
def google_finish(body: GoogleFinishIn, subject: Subject = Depends(current_subject),
                  session: Session = Depends(get_session)) -> MailboxOut:
    from app.mail import oauth

    code, state = _oauth_call(oauth.code_from, body.url)
    data = _oauth_call(oauth.google_finish, subject.id, code, state)
    return _save_signed_in(session, subject.id, data)


@router.get("/me/mailbox/oauth/google/callback", include_in_schema=False)
def google_callback(state: str = "", code: str = "", error: str = "",
                    session: Session = Depends(get_session)) -> Response:
    """Where Google sends the browser when the app has a stable redirect_uri (see oauth.py).
    No sign-in token comes with a redirect, so the person is found by the sign-in's state."""
    from app.mail import oauth

    def page(text: str, code_: int = 200) -> Response:
        return Response(text, status_code=code_, media_type="text/plain; charset=utf-8")

    owner = oauth.owner_of_state(state) if state else None
    if owner is None:
        return page("This sign-in has expired. Go back to the app and start it again.", 400)
    if error:
        return page(f"Google said: {error}. Go back to the app and try again.", 400)
    try:
        data = oauth.google_finish(owner, code, state)
        _save_signed_in(session, owner, data)
    except oauth.OAuthError as e:
        return page(f"{e}. Go back to the app and try again.", 400)
    except HTTPException as e:
        return page(f"{e.detail} Go back to the app and try again.", 400)
    return page("Your Gmail is connected. You can close this tab and go back to the app.")


@router.post("/me/mailbox/oauth/microsoft/start", response_model=MicrosoftStartOut)
def microsoft_start(subject: Subject = Depends(current_subject)) -> MicrosoftStartOut:
    from app.mail import oauth

    return MicrosoftStartOut(**_oauth_call(oauth.microsoft_start, subject.id))


@router.post("/me/mailbox/oauth/microsoft/poll", response_model=MicrosoftPollOut)
def microsoft_poll(subject: Subject = Depends(current_subject), session: Session = Depends(get_session)) -> MicrosoftPollOut:
    from app.mail import oauth

    data = _oauth_call(oauth.microsoft_poll, subject.id)
    if data is None:
        return MicrosoftPollOut(done=False)
    return MicrosoftPollOut(done=True, mailbox=_save_signed_in(session, subject.id, data))


@router.delete("/me/mailbox", status_code=status.HTTP_204_NO_CONTENT)
def delete_mailbox(subject: Subject = Depends(current_subject), session: Session = Depends(get_session)) -> None:
    AESGCMVault(session).delete(_secret(subject))


class DigestOut(BaseModel):
    text: str


@router.post("/me/digest", response_model=DigestOut)
def digest_now(subject: Subject = Depends(current_subject), session: Session = Depends(get_session)) -> DigestOut:
    """Build the mail digest now (it also lands under the bell)."""
    from app.reminders import digest

    user = _user(session, subject)
    if AESGCMVault(session).get(_secret(subject)) is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "connect your mailbox first")
    return DigestOut(text=digest.run_for(session, user))


# ---- notifications ----
class NotificationOut(BaseModel):
    id: str
    kind: str
    title: str
    body: str
    read: bool
    created_at: dt.datetime


class CountOut(BaseModel):
    unread: int


@router.get("/notifications", response_model=list[NotificationOut])
def list_notifications(
    unread_only: bool = False, subject: Subject = Depends(current_subject), session: Session = Depends(get_session),
) -> list[NotificationOut]:
    return [NotificationOut(**n) for n in service.notifications(session, subject.id, unread_only=unread_only)]


@router.get("/notifications/count", response_model=CountOut)
def notification_count(subject: Subject = Depends(current_subject), session: Session = Depends(get_session)) -> CountOut:
    return CountOut(unread=service.unread_count(session, subject.id))


class ReadIn(BaseModel):
    id: str | None = None  # None = all


@router.post("/notifications/read", response_model=CountOut)
def read_notifications(body: ReadIn, subject: Subject = Depends(current_subject), session: Session = Depends(get_session)) -> CountOut:
    service.mark_read(session, subject.id, body.id)
    return CountOut(unread=service.unread_count(session, subject.id))


# ---- reminders ----
class ReminderIn(BaseModel):
    when: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=service.MAX_TEXT)
    repeat: Literal["none", "daily", "weekdays", "weekly", "monthly"] = "none"
    email: bool = False


class ReminderOut(BaseModel):
    id: str
    text: str
    due_at: dt.datetime
    due: str
    repeat: str
    email: bool
    source: str


@router.get("/reminders", response_model=list[ReminderOut])
def list_reminders(subject: Subject = Depends(current_subject), session: Session = Depends(get_session)) -> list[ReminderOut]:
    return [ReminderOut(**r) for r in service.pending(session, subject.id)]


@router.post("/reminders", response_model=ReminderOut, status_code=status.HTTP_201_CREATED)
def add_reminder(body: ReminderIn, subject: Subject = Depends(current_subject), session: Session = Depends(get_session)) -> ReminderOut:
    try:
        row, _ = service.create(session, subject.id, body.when, body.text, repeat=body.repeat, email=body.email, source="app")
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
    return next(ReminderOut(**r) for r in service.pending(session, subject.id) if r["id"] == row.id)


@router.delete("/reminders/{reminder_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_reminder(reminder_id: str, subject: Subject = Depends(current_subject), session: Session = Depends(get_session)) -> None:
    if not service.cancel(session, subject.id, reminder_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "reminder not found")


# ---- My files ----
class FileOut(BaseModel):
    id: str
    path: str
    size: int
    content_type: str
    version: int
    versions: int
    updated_at: dt.datetime
    deleted_at: dt.datetime | None = None


class FilesOut(BaseModel):
    files: list[FileOut]
    used: int
    quota: int


@router.get("/files", response_model=FilesOut)
def list_files(
    trash: bool = False, subject: Subject = Depends(current_subject), key: bytes = Depends(chat_key),
    session: Session = Depends(get_session),
) -> FilesOut:
    items = userfiles.list_files(session, key, subject.id, trash=trash)
    return FilesOut(
        files=[FileOut(**f.as_dict()) for f in items], used=userfiles.used_bytes(session, subject.id),
        quota=userfiles.QUOTA_BYTES,
    )


@router.post("/files", response_model=FileOut, status_code=status.HTTP_201_CREATED)
def upload_file(
    file: UploadFile, folder: str = Form(default="", max_length=200), subject: Subject = Depends(current_subject),
    key: bytes = Depends(chat_key), session: Session = Depends(get_session),
) -> FileOut:
    from app.api.routes_chat import safe_filename
    from app.request_manager import guards
    from app.storage.attachments import extract_text

    limit = get_settings().attachment_max_bytes
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, f"files are limited to {limit} bytes")
    name = safe_filename(file.filename)
    path = f"{folder.strip().strip('/')}/{name}" if folder.strip().strip("/") else name
    content_type = file.content_type or userfiles.guess_type(name)
    text = extract_text(name, content_type, data)
    if text:
        flagged, why = guards.is_nsfw(text[:NSFW_SAMPLE_CHARS])
        if flagged:
            guards.report_nsfw(session, subject.id, f"[file {name}] {text[:2000]}")
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{why}; reported to hr, admin")
    try:
        info, _ = userfiles.write(session, key, subject.id, path, data, content_type)
    except userfiles.FileError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
    return FileOut(**info.as_dict())


@router.get("/files/{file_id}")
def download_file(
    file_id: str, subject: Subject = Depends(current_subject), key: bytes = Depends(chat_key),
    session: Session = Depends(get_session),
) -> Response:
    found = userfiles.read_by_id(session, key, subject.id, file_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    info, data = found
    name = info.path.rsplit("/", 1)[-1]
    # Always a download of opaque bytes, so a stored file can never run as a page here.
    return Response(
        content=data, media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}", "X-Content-Type-Options": "nosniff"},
    )


@router.delete("/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(file_id: str, subject: Subject = Depends(current_subject), session: Session = Depends(get_session)) -> None:
    if not userfiles.delete_by_id(session, subject.id, file_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")


@router.post("/files/{file_id}/restore", response_model=FileOut)
def restore_file(
    file_id: str, subject: Subject = Depends(current_subject), key: bytes = Depends(chat_key),
    session: Session = Depends(get_session),
) -> FileOut:
    info = userfiles.by_id(session, key, subject.id, file_id)
    if info is None or info.deleted_at is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such file in the trash")
    if any(f.path == info.path for f in userfiles.list_files(session, key, subject.id)):
        raise HTTPException(status.HTTP_409_CONFLICT, f"a file named '{info.path}' already exists")
    userfiles.undelete_by_id(session, subject.id, file_id)
    return FileOut(**userfiles.by_id(session, key, subject.id, file_id).as_dict())


# ---- saved details (app/wallet.py): only the owner, with their key ----
class WalletIn(BaseModel):
    kind: Literal["login", "card", "upi", "address"]
    data: dict[str, str]


class WalletOut(BaseModel):
    id: str
    kind: str
    summary: str
    site: str = ""
    label: str = ""


class RevealIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)


@router.get("/me/wallet", response_model=list[WalletOut])
def list_wallet(subject: Subject = Depends(current_subject), key: bytes = Depends(chat_key),
                session: Session = Depends(get_session)) -> list[WalletOut]:
    from app import wallet

    return [WalletOut(**i) for i in wallet.listing(session, key, subject.id)]


@router.post("/me/wallet", response_model=WalletOut, status_code=status.HTTP_201_CREATED)
def add_wallet(body: WalletIn, subject: Subject = Depends(current_subject), key: bytes = Depends(chat_key),
               session: Session = Depends(get_session)) -> WalletOut:
    from app import wallet

    try:
        item_id = wallet.add(session, key, subject.id, body.kind, body.data)
    except wallet.WalletError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
    return next(WalletOut(**i) for i in wallet.listing(session, key, subject.id) if i["id"] == item_id)


@router.delete("/me/wallet/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_wallet(item_id: str, subject: Subject = Depends(current_subject), session: Session = Depends(get_session)) -> None:
    from app import wallet

    if not wallet.delete(session, subject.id, item_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


@router.post("/me/wallet/{item_id}/reveal")
def reveal_wallet(item_id: str, body: RevealIn, subject: Subject = Depends(current_subject),
                  key: bytes = Depends(chat_key), session: Session = Depends(get_session)) -> dict:
    """The whole saved detail, after the person types their account password again."""
    from app import wallet
    from app.auth.security import verify_password

    user = _user(session, subject)
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "wrong password")
    try:
        kind, data = wallet.get(session, key, subject.id, item_id)
    except wallet.WalletError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from None
    return {"kind": kind, **data}
