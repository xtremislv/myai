"""WhatsApp: Meta's webhook, linking a number in Settings, and the per-person result page (app/whatsapp/)."""
from __future__ import annotations

import hmac
import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.acl.models import Subject
from app.api.deps import current_subject
from app.config import get_settings
from app.db.session import SessionLocal, get_session
from app.whatsapp import bot, cloud, links

router = APIRouter(tags=["whatsapp"])
MAX_BODY = 1_000_000


# ---- Meta's webhook (public; every call is checked against the app secret) ----
@router.get("/v1/whatsapp/webhook", include_in_schema=False)
def verify_webhook(request: Request, session: Session = Depends(get_session)) -> PlainTextResponse:
    """Meta's one-time check when the callback URL is saved in the app's WhatsApp settings."""
    params = request.query_params
    cfg = cloud.config(session)
    # TEMPORARY (owner, 2026-11-17): WHATSAPP_VERIFY_TOKEN lets Meta's check pass before the whatsapp/cloud secret
    # exists. Verification only: messages still need the secret (signature). Remove with the env key once it's added.
    expected = cfg.verify_token if cfg is not None else get_settings().whatsapp_verify_token
    if (not expected or params.get("hub.mode") != "subscribe"
            or not hmac.compare_digest(params.get("hub.verify_token", ""), expected)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "verification failed")
    return PlainTextResponse(params.get("hub.challenge", ""))


def _config() -> cloud.Config | None:
    with SessionLocal() as session:
        return cloud.config(session)


@router.post("/v1/whatsapp/webhook", include_in_schema=False)
async def receive_webhook(request: Request) -> dict:
    """Answered at once (Meta retries slow answers); the messages are handled in the background."""
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "too large")
    cfg = await run_in_threadpool(_config)
    if cfg is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "WhatsApp is not set up on this server")
    if not cloud.signature_ok(cfg.app_secret, body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad signature")
    try:
        payload = json.loads(body)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "not JSON") from e
    await run_in_threadpool(bot.handle_payload, payload)
    return {"ok": True}


# ---- Settings: link a number ----
class WhatsAppOut(BaseModel):
    configured: bool
    bot_number: str = ""
    linked: bool = False
    phone: str = ""
    pending_phone: str = ""
    notify: bool = True
    page_url: str = ""


class LinkIn(BaseModel):
    phone: str = Field(min_length=6, max_length=32)


class LinkOut(BaseModel):
    code: str
    phone: str
    bot_number: str
    chat_link: str  # opens WhatsApp with "LINK <code>" typed in, to the bot's number
    minutes: int


class NotifyIn(BaseModel):
    notify: bool


def _status(session: Session, user_id: str) -> WhatsAppOut:
    cfg = cloud.config(session)
    row = links.get(session, user_id)
    out = WhatsAppOut(configured=cfg is not None, bot_number=cfg.display_number if cfg else "")
    if row is None:
        return out
    linked = bool(row.phone)
    return out.model_copy(update={
        "linked": linked, "phone": row.phone or "", "pending_phone": row.pending_phone or "", "notify": row.notify,
        "page_url": links.page_url(session, row) if linked and links.base_url() else "",
    })


@router.get("/v1/me/whatsapp", response_model=WhatsAppOut)
def whatsapp_status(subject: Subject = Depends(current_subject), session: Session = Depends(get_session)) -> WhatsAppOut:
    return _status(session, subject.id)


@router.post("/v1/me/whatsapp", response_model=LinkOut)
def start_link(body: LinkIn, subject: Subject = Depends(current_subject),
               session: Session = Depends(get_session)) -> LinkOut:
    cfg = cloud.config(session)
    if cfg is None or not cfg.display_number:
        raise HTTPException(status.HTTP_409_CONFLICT, "WhatsApp isn't set up on this server yet: ask your administrator.")
    try:
        code = links.start(session, subject.id, body.phone)
    except links.LinkError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    row = links.get(session, subject.id)
    return LinkOut(code=code, phone=row.pending_phone or "", bot_number=cfg.display_number,
                   chat_link=f"https://wa.me/{cfg.display_number}?text={quote(f'LINK {code}')}", minutes=links.CODE_MINUTES)


@router.put("/v1/me/whatsapp", response_model=WhatsAppOut)
def set_notify(body: NotifyIn, subject: Subject = Depends(current_subject),
               session: Session = Depends(get_session)) -> WhatsAppOut:
    row = links.get(session, subject.id)
    if row is None or not row.phone:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no WhatsApp number is linked")
    row.notify = body.notify
    session.commit()
    return _status(session, subject.id)


@router.delete("/v1/me/whatsapp", status_code=status.HTTP_204_NO_CONTENT)
def unlink(subject: Subject = Depends(current_subject), session: Session = Depends(get_session)) -> Response:
    links.unlink(session, subject.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/v1/me/whatsapp/page/reset", response_model=WhatsAppOut)
def reset_page(subject: Subject = Depends(current_subject), session: Session = Depends(get_session)) -> WhatsAppOut:
    row = links.get(session, subject.id)
    if row is None or not row.phone:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no WhatsApp number is linked")
    links.reset_page(session, row)
    return _status(session, subject.id)


# ---- the result page's data (the secret link is the only key: owner's choice, 2026-11-17) ----
class PageOut(BaseModel):
    title: str = ""
    markdown: str = ""
    dashboards: list[dict] = Field(default_factory=list)
    updated_at: str | None = None


@router.get("/v1/whatsapp/page/{token}", response_model=PageOut, include_in_schema=False)
def page_data(token: str, response: Response, session: Session = Depends(get_session)) -> PageOut:
    data = links.load_page(session, token)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This link doesn't exist, or it was replaced by a new one.")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex"
    response.headers["Referrer-Policy"] = "no-referrer"
    return PageOut(**data)
