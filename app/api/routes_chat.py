"""The chat box: one conversation that triages each message (guards.triage).

Private messages: no endpoint exposes them to anyone but their owner, and they have no
tools, audit rows or content logging. Text and attachments are encrypted at rest with the
user's own key (app/vault/user_keys.py). The only thing that ever leaves this channel is
content flagged as explicit or illegal, reported to hr and admin.

Work messages go through the Request Manager, with its tools, quotas, audit and approvals.
That run is stored apart from the encrypted chat, because an approver has to resume it
without the user's key. The exchange is then copied into the encrypted chat, so the
conversation reads as one, marked with the department that handled it.
"""
from __future__ import annotations

import contextvars
import datetime as dt
import json
import logging
import queue
import re
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal
from urllib.parse import quote

from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, Form, HTTPException, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.acl.models import Subject
from app.api.deps import chat_key, current_subject
from app.config import get_settings
from app.db.session import (
    Approval,
    ChatThread,
    DepartmentTask,
    get_session,
    release_connection,
)
from app.departments.packs import DEFAULT_DEPARTMENTS
from app.orchestrator import execution
from app.orchestrator.execution import scoped_thread
from app.orchestrator.progress import progress_sink, step
from app.request_manager import approvals as approvals_store
from app.request_manager import concurrency as concurrency_limits
from app.request_manager import guards
from app.request_manager.router import RMRequest, RMResult, handle_request
from app.storage import attachments
from app.vault import user_keys

router = APIRouter(prefix="/v1/chat", tags=["chat"])
logger = logging.getLogger(__name__)

BUSY_MESSAGE = "The assistant is busy with other replies right now. Try again in a moment."

TITLE_CHARS = 60
THREAD_ID_MAX = 100  # stored with a suffix when a paused run needs a fresh one
DEPARTMENT_LABELS = {"account": "Accounts", "hr": "HR", "logistics": "Logistics", "researcher": "Research",
                     "superuser": "Superuser", "admin": "Admin"}
RECENT_MESSAGES = 2  # what triage sees
WORK_CONTEXT_MESSAGES = 12  # how far back a work request looks for earlier work in this chat
REPORT_EXCERPT_CHARS = 2000
NSFW_SAMPLE_CHARS = 4000


class ChatIn(BaseModel):
    message: str
    thread_id: str = Field(min_length=1, max_length=THREAD_ID_MAX)  # private to the caller
    # "auto" lets triage decide. "single" = one agent loop; "team" = planner -> researcher -> writer;
    # "agents" = a supervisor handing tasks to specialists (research, browser, mail, booking...).
    mode: Literal["auto", "single", "team", "agents"] = "auto"


class ConfirmOut(BaseModel):
    """An action the assistant wants to take in a private chat, shown for the person to confirm.
    Sent to the owner only, never stored outside their encrypted chat."""

    tool: str
    title: str
    details: dict[str, str]


class ConfirmIn(BaseModel):
    approve: bool


CONFIRM_TITLES = {
    "send_mail": "Send this email?",
    "delete_file": "Delete this file?",
    "browser_commit": "Confirm this on the website?",
}


def confirm_out(pending: dict) -> ConfirmOut:
    args = pending.get("args") or {}
    shown = {k: str(v)[:4000] for k, v in args.items() if v not in ("", None)}
    return ConfirmOut(tool=pending["tool"], title=CONFIRM_TITLES.get(pending["tool"], f"Allow {pending['tool']}?"), details=shown)


def _private_reply(thread_id: str, mode: str, out) -> ChatOut:
    if isinstance(out, execution.PendingAction):
        return ChatOut(
            thread_id=thread_id, reply="", mode=mode, outcome="confirm",
            confirm=confirm_out({"tool": out.tool, "args": out.args}),
        )
    return ChatOut(thread_id=thread_id, reply=out, mode=mode)


class ChatOut(BaseModel):
    thread_id: str
    reply: str
    mode: str
    outcome: str = "ok"  # ok | flagged_nsfw | forwarded | pending_approval | denied_auth | ...
    reason: str = ""
    route: Literal["private", "work"] = "private"
    department: str | None = None
    approval_id: str | None = None
    task_id: str | None = None
    # A private action waiting on the person's own go-ahead (outcome "confirm").
    confirm: ConfirmOut | None = None


class ThreadOut(BaseModel):
    thread_id: str
    title: str
    updated_at: dt.datetime


class MessageOut(BaseModel):
    role: str
    content: str
    handled_by: str | None = None  # the department, for work; private messages have none


class ChatRequestOut(BaseModel):
    """Progress of work asked for in a chat."""

    id: str
    kind: Literal["approval", "task"]
    department: str
    label: str
    state: str
    result: str | None = None


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


def _before_the_model(
    body: ChatIn, subject: Subject, key: bytes, session: Session
) -> tuple[str, ChatOut | None]:
    """Everything up to generating a private reply. Returns the mode, plus a finished answer
    if nothing is left to generate: a flagged message, or work."""
    history = execution.private_history(subject, body.thread_id, key)[-WORK_CONTEXT_MESSAGES:]
    recent = history[-RECENT_MESSAGES:]
    mine = _departments_of(subject)
    others = {d.name: d.description for d in DEFAULT_DEPARTMENTS if d.name not in mine and d.name != "admin"}
    sorted_message = guards.triage(body.message, mine, recent, others)
    mode = sorted_message.mode if body.mode == "auto" else body.mode

    if sorted_message.nsfw:
        guards.report_nsfw(session, subject.id, body.message)
        return mode, ChatOut(
            thread_id=body.thread_id, reply="", mode=mode, outcome="flagged_nsfw",
            reason=f"{sorted_message.why}; reported to hr, admin",
        )

    if sorted_message.route == "work":
        step(f"Passing this to {DEPARTMENT_LABELS.get(sorted_message.department or 'account', 'the team')}"
             if not sorted_message.action else "Passing this to the accounts team")
        result = handle_request(
            session,
            subject,
            RMRequest(
                message=body.message,
                thread_id=_work_thread(session, subject.id, body.thread_id),
                mode=mode,
                department=sorted_message.department,
                chat_thread=body.thread_id,
                prechecked=True,
                forward_kind=sorted_message.action,
                context=_work_turns(history),
            ),
        )
        reply = _work_reply(result)
        execution.record_private(subject, body.thread_id, key, body.message, reply, result.department)
        _remember_thread(session, key, subject.id, body.thread_id, body.message)
        return mode, ChatOut(
            thread_id=body.thread_id, reply=reply, mode=mode, outcome=result.outcome, route="work",
            department=result.department, approval_id=result.approval_id, task_id=result.task_id,
        )
    return mode, None


@contextmanager
def _private_slot(subject: Subject) -> Iterator[bool]:
    """A private reply needs a slot for this person and one in the shared pool, so one
    person can't take the whole GPU."""
    settings = get_settings()
    slots = concurrency_limits.default_concurrency()
    with slots.slot(f"user:{subject.id}", settings.private_max_per_user) as mine:
        with slots.slot("private", settings.private_max_total) as shared:
            yield mine and shared


@router.post("", response_model=ChatOut)
def chat(
    body: ChatIn,
    subject: Subject = Depends(current_subject),
    key: bytes = Depends(chat_key),
    session: Session = Depends(get_session),
) -> ChatOut:
    mode, finished = _before_the_model(body, subject, key, session)
    if finished is not None:
        return finished

    context = attachments.context_for_thread(session, key, subject.id, body.thread_id)
    files = attachments.files_for_thread(session, key, subject.id, body.thread_id)
    release_connection(session)
    with _private_slot(subject) as free:
        if not free:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, BUSY_MESSAGE)
        reply = execution.run(subject, body.message, body.thread_id, mode, None, chat_key=key,
                              attachments=context, files=files)
    _remember_thread(session, key, subject.id, body.thread_id, body.message)
    return _private_reply(body.thread_id, mode, reply)


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _stream_response(events: Iterator[str]) -> StreamingResponse:
    # X-Accel-Buffering stops proxies holding the words back until the reply ends.
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/stream")
def chat_stream(
    body: ChatIn,
    subject: Subject = Depends(current_subject),
    key: bytes = Depends(chat_key),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """Like POST /v1/chat, as server-sent events: {"type":"step","text":...} for each thing the
    agents do ("Checking inventory for laptop"), {"type":"delta","text":...} as the words of a
    private reply arrive, then {"type":"done",...the ChatOut fields...} or {"type":"error",...}.
    Work replies arrive whole, after their steps."""
    # The request's own session closes once the response starts, so the work runs on its own.
    bind = session.get_bind()
    release_connection(session)
    prepared: queue.Queue = queue.Queue()

    def prepare() -> None:
        progress_sink.set(lambda text: prepared.put(("step", text)))
        try:
            with Session(bind=bind) as own:
                prepared.put(("ready", _before_the_model(body, subject, key, own)))
        except Exception as e:  # noqa: BLE001 - re-raised or reported below
            prepared.put(("fail", e))

    threading.Thread(target=contextvars.copy_context().run, args=(prepare,), daemon=True).start()
    first = prepared.get()
    if first[0] == "fail":
        raise first[1]  # nothing sent yet: a refusal keeps its HTTP status

    def events() -> Iterator[str]:
        event = first
        while event[0] == "step":  # live, while the work runs
            yield _event({"type": "step", "text": event[1]})
            event = prepared.get()
        if event[0] == "fail":
            # The type only: an exception's text could quote the message.
            logger.error("chat stream failed: %s", type(event[1]).__name__)
            yield _event({"type": "error", "detail": "That request failed on the server. Try again."})
            return
        mode, finished = event[1]
        if finished is not None:
            yield _event({"type": "done", **finished.model_dump()})
            return

        with Session(bind=bind) as own:
            context = attachments.context_for_thread(own, key, subject.id, body.thread_id)
            files = attachments.files_for_thread(own, key, subject.id, body.thread_id)
        with _private_slot(subject) as free:
            if not free:
                yield _event({"type": "error", "detail": BUSY_MESSAGE})
                return
            for kind, value in execution.stream_private(
                subject, body.message, body.thread_id, mode, key, context, files
            ):
                if kind == "delta":
                    yield _event({"type": "delta", "text": value})
                elif kind == "step":
                    yield _event({"type": "step", "text": value})
                elif kind == "error":
                    logger.error("chat stream failed: %s", type(value).__name__)
                    yield _event({"type": "error", "detail": "That reply failed on the server. Try again."})
                    return
                else:
                    reply = value
        with Session(bind=bind) as later:
            _remember_thread(later, key, subject.id, body.thread_id, body.message)
        yield _event({"type": "done", **_private_reply(body.thread_id, mode, reply).model_dump()})

    return _stream_response(events())


def _departments_of(subject: Subject) -> dict[str, str]:
    """The departments this person can work in, for triage. The admin can act for any."""
    roles = set(subject.roles)
    return {
        d.name: d.description
        for d in DEFAULT_DEPARTMENTS
        if "admin" in roles or d.name in roles
    }


def _work_turns(history: list[dict]) -> list[dict]:
    """The exchanges in this chat that were handled as work: each reply marked handled_by,
    with the request just before it. Those already left the encrypted chat when they were
    sent; private turns never do."""
    turns = []
    for before, reply in zip(history, history[1:]):
        if reply.get("role") == "assistant" and reply.get("handled_by") and before.get("role") == "user":
            turns += [{"role": "user", "content": before["content"]},
                      {"role": "assistant", "content": reply["content"]}]
    return turns


def _work_thread(session: Session, subject_id: str, thread_id: str) -> str:
    """A run paused on an approval cannot take new work, but the chat should carry on, so
    new work starts a fresh run."""
    if approvals_store.pending_for_thread(session, scoped_thread(subject_id, thread_id)) is None:
        return thread_id
    return f"{thread_id}~{uuid.uuid4().hex[:8]}"


def _work_reply(result: RMResult) -> str:
    department = (result.department or "that department").title()
    match result.outcome:
        case "ok":
            return result.reply or "Done."
        case "forwarded":
            return "I sent this to the accounts team. You can follow it here."
        case "pending_approval":
            return "This needs approval before it can go ahead. You can follow it here."
        case "denied_busy":
            return f"{department} is busy with other requests right now. Try again in a moment."
        case "denied_quota":
            return f"{department} has too many requests right now. Try again in a minute."
        case "denied_auth":
            return (
                "This looks like work for a department your account isn't part of. "
                "Ask your administrator if you need access."
            )
        case _:
            return "That didn't work because of a problem on the server. Try again in a moment."


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
    threads = []
    for r in rows:
        try:
            title = user_keys.decrypt_text(key, r.encrypted_title, f"{subject.id}:{r.thread_id}")
        except InvalidTag:
            # Encrypted under an earlier chat key (the admin reset this password and the user
            # chose a new one): nobody can open it any more, so don't list it.
            continue
        threads.append(ThreadOut(thread_id=r.thread_id, title=title, updated_at=r.updated_at))
    return threads


@router.get("/threads/{thread_id}/messages", response_model=list[MessageOut])
def thread_messages(
    thread_id: str,
    subject: Subject = Depends(current_subject),
    key: bytes = Depends(chat_key),
) -> list[MessageOut]:
    return [MessageOut(**m) for m in execution.private_history(subject, thread_id, key)]


@router.get("/threads/{thread_id}/pending", response_model=ConfirmOut | None)
def thread_pending(
    thread_id: str,
    subject: Subject = Depends(current_subject),
    key: bytes = Depends(chat_key),
) -> ConfirmOut | None:
    """The action this private chat is waiting for you to confirm, if any."""
    pending = execution.private_pending(subject, thread_id, key)
    return confirm_out(pending) if pending else None


@router.post("/threads/{thread_id}/confirm", response_model=ChatOut)
def thread_confirm(
    thread_id: str,
    body: ConfirmIn,
    subject: Subject = Depends(current_subject),
    key: bytes = Depends(chat_key),
) -> ChatOut:
    """Go ahead with (or decline) the action the assistant asked you to confirm."""
    pending = execution.private_pending(subject, thread_id, key)
    if pending is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "nothing is waiting for your confirmation in this chat")
    with _private_slot(subject) as free:
        if not free:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, BUSY_MESSAGE)
        try:
            out = execution.resume_private(
                subject, thread_id, key, approved=body.approve, tool=pending["tool"], args=pending["args"],
                mode=pending["mode"], reason="" if body.approve else "the person said no",
            )
        except execution.StaleApproval as e:
            raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    return _private_reply(thread_id, pending["mode"], out)


@router.get("/threads/{thread_id}/requests", response_model=list[ChatRequestOut])
def thread_requests(
    thread_id: str,
    subject: Subject = Depends(current_subject),
    session: Session = Depends(get_session),
) -> list[ChatRequestOut]:
    approvals = (
        session.query(Approval)
        .filter(Approval.subject_id == subject.id, Approval.chat_thread == thread_id, Approval.task_id.is_(None))
        .order_by(Approval.created_at)
        .all()
    )
    tasks = (
        session.query(DepartmentTask)
        .filter(DepartmentTask.requester_id == subject.id, DepartmentTask.chat_thread == thread_id)
        .order_by(DepartmentTask.created_at)
        .all()
    )
    return [
        *[
            ChatRequestOut(
                id=a.id, kind="approval", department=a.department, label=a.tool, state=a.state, result=a.result
            )
            for a in approvals
        ],
        *[
            ChatRequestOut(
                id=t.id, kind="task", department=t.department, label=t.kind, state=t.state, result=t.result
            )
            for t in tasks
        ],
    ]


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
