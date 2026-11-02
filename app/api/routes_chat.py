"""Private chat: a conversation between one user and the AI.

No endpoint exposes these conversations to anyone but their owner, and they have no
tools, audit rows or content logging. Text and attachments are encrypted at rest with
the user's own key (app/vault/user_keys.py). The only thing that ever leaves this
channel is content flagged as explicit or illegal, reported to hr and admin.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.acl.models import Subject
from app.api.deps import chat_key, current_subject
from app.config import get_settings
from app.db.session import ChatThread, get_session, release_connection
from app.orchestrator import execution
from app.request_manager import guards
from app.storage import attachments
from app.vault import user_keys

router = APIRouter(prefix="/v1/chat", tags=["chat"])

TITLE_CHARS = 60
THREAD_ID_MAX = 128
REPORT_EXCERPT_CHARS = 2000
NSFW_SAMPLE_CHARS = 4000


class ChatIn(BaseModel):
    message: str
    thread_id: str = Field(min_length=1, max_length=THREAD_ID_MAX)  # private to the caller
    # "single" = one agent loop; "team" = planner -> researcher -> writer.
    mode: Literal["single", "team"] = "single"


class ChatOut(BaseModel):
    thread_id: str
    reply: str
    mode: str
    outcome: Literal["ok", "flagged_nsfw"] = "ok"
    reason: str = ""


class ThreadOut(BaseModel):
    thread_id: str
    title: str
    updated_at: dt.datetime


class MessageOut(BaseModel):
    role: str
    content: str


class AttachmentOut(BaseModel):
    id: str
    thread_id: str
    name: str
    content_type: str
    size: int
    created_at: dt.datetime


class UploadOut(BaseModel):
    outcome: Literal["ok", "flagged_nsfw"] = "ok"
    reason: str = ""
    attachment: AttachmentOut | None = None


def safe_filename(name: str | None) -> str:
    """Names come from the client: keep only the last path part and printable characters."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"[\x00-\x1f\x7f]", "", base)[:200] or "file"


def _remember_thread(session: Session, key: bytes, owner_id: str, thread_id: str, message: str) -> None:
    row = session.get(ChatThread, (owner_id, thread_id))
    if row is None:
        title = user_keys.encrypt_text(key, message[:TITLE_CHARS], f"{owner_id}:{thread_id}")
        session.add(ChatThread(owner_id=owner_id, thread_id=thread_id, encrypted_title=title))
    else:
        row.updated_at = dt.datetime.now(dt.timezone.utc)
    session.commit()


@router.post("", response_model=ChatOut)
def chat(
    body: ChatIn,
    subject: Subject = Depends(current_subject),
    key: bytes = Depends(chat_key),
    session: Session = Depends(get_session),
) -> ChatOut:
    flagged, why = guards.is_nsfw(body.message)
    if flagged:
        guards.report_nsfw(session, subject.id, body.message)
        return ChatOut(
            thread_id=body.thread_id, reply="", mode=body.mode, outcome="flagged_nsfw",
            reason=f"{why}; reported to hr, admin",
        )
    context = attachments.context_for_thread(session, key, subject.id, body.thread_id)
    release_connection(session)
    reply = execution.run(
        subject, body.message, body.thread_id, body.mode, None, chat_key=key, attachments=context
    )
    _remember_thread(session, key, subject.id, body.thread_id, body.message)
    return ChatOut(thread_id=body.thread_id, reply=reply, mode=body.mode)


@router.get("/threads", response_model=list[ThreadOut])
def list_threads(
    subject: Subject = Depends(current_subject),
    key: bytes = Depends(chat_key),
    session: Session = Depends(get_session),
) -> list[ThreadOut]:
    rows = (
        session.query(ChatThread)
        .filter(ChatThread.owner_id == subject.id)
        .order_by(ChatThread.updated_at.desc())
        .all()
    )
    return [
        ThreadOut(
            thread_id=r.thread_id,
            title=user_keys.decrypt_text(key, r.encrypted_title, f"{subject.id}:{r.thread_id}"),
            updated_at=r.updated_at,
        )
        for r in rows
    ]


@router.get("/threads/{thread_id}/messages", response_model=list[MessageOut])
def thread_messages(
    thread_id: str,
    subject: Subject = Depends(current_subject),
    key: bytes = Depends(chat_key),
) -> list[MessageOut]:
    return [MessageOut(**m) for m in execution.private_history(subject, thread_id, key)]


@router.post("/attachments", response_model=UploadOut)
def upload_attachment(
    file: UploadFile,
    thread_id: str = Form(min_length=1, max_length=THREAD_ID_MAX),
    subject: Subject = Depends(current_subject),
    key: bytes = Depends(chat_key),
    session: Session = Depends(get_session),
) -> UploadOut:
    limit = get_settings().attachment_max_bytes
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"files are limited to {limit} bytes")
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the file is empty")
    name = safe_filename(file.filename)
    content_type = file.content_type or "application/octet-stream"

    text = attachments.extract_text(name, content_type, data)
    if text:
        flagged, why = guards.is_nsfw(text[:NSFW_SAMPLE_CHARS])
        if flagged:
            guards.report_nsfw(session, subject.id, f"[attachment {name}] {text[:REPORT_EXCERPT_CHARS]}")
            return UploadOut(outcome="flagged_nsfw", reason=f"{why}; reported to hr, admin")

    saved = attachments.save(session, key, subject.id, thread_id, name, content_type, data)
    return UploadOut(attachment=AttachmentOut(**saved))


@router.get("/attachments", response_model=list[AttachmentOut])
def list_attachments(
    thread_id: str | None = None,
    subject: Subject = Depends(current_subject),
    key: bytes = Depends(chat_key),
    session: Session = Depends(get_session),
) -> list[AttachmentOut]:
    return [AttachmentOut(**a) for a in attachments.list_for(session, key, subject.id, thread_id)]


@router.get("/attachments/{attachment_id}")
def download_attachment(
    attachment_id: str,
    subject: Subject = Depends(current_subject),
    key: bytes = Depends(chat_key),
    session: Session = Depends(get_session),
) -> Response:
    found = attachments.read(session, key, subject.id, attachment_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "attachment not found")
    meta, data = found
    # Always a download of opaque bytes, so an uploaded file can never run as a page here.
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(meta['name'])}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_attachment(
    attachment_id: str,
    subject: Subject = Depends(current_subject),
    session: Session = Depends(get_session),
) -> None:
    if not attachments.delete(session, subject.id, attachment_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "attachment not found")
