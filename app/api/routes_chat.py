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
from app.orchestrator.context import browser_request, dashboards
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
WORK_CONTEXT_MESSAGES = 30  # how far back a work request looks for earlier work in this chat
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
    purchase: bool = False  # an order or payment: the card asks "personal or work?"


class ConfirmIn(BaseModel):
    approve: bool
    # For an order: the person said it is for work, so it goes to the Accounts team instead.
    as_work: bool = False


CONFIRM_TITLES = {
    "send_mail": "Send this email?",
    "delete_file": "Delete this file?",
    "browser_commit": "Go ahead on the website?",
    "share_with": "Share this?",
}


MAILBOX_LABELS = {"finance": "finance (Accounts' official mailbox)", "career": "careers (HR's official mailbox)",
                  "contact": "contact (Admin's official mailbox)"}


def confirm_out(pending: dict) -> ConfirmOut:
    args = pending.get("args") or {}
    shown = {k: str(v)[:4000] for k, v in args.items() if v not in ("", None) and k != "category"}
    if pending["tool"] == "send_mail":
        box = str(args.get("category") or "personal").lower()
        shown = {"from": MAILBOX_LABELS.get(box, "your own mailbox"), **shown}
    purchase = pending["tool"] == "browser_commit" and bool(PURCHASE_WORDS.search(f"{args.get('label', '')} {args.get('summary', '')}"))
    title = "Place this order?" if purchase else CONFIRM_TITLES.get(pending["tool"], f"Allow {pending['tool']}?")
    if pending["tool"] == "share_with":
        title = f"Share this with {args.get('to', 'them')}?"
    return ConfirmOut(tool=pending["tool"], title=title, details=shown, purchase=purchase)


PURCHASE_WORDS = re.compile(r"\b(order|buy|pay|purchase|checkout|place)\b", re.IGNORECASE)


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
    # The agent handed the browser to the person: {"scope", "reason"} (see routes_browser).
    browser: dict | None = None
    # Dashboards made for this reply (app/dashboards.py), drawn under it.
    dashboards: list[dict] = Field(default_factory=list)


class ThreadOut(BaseModel):
    thread_id: str
    title: str
    updated_at: dt.datetime


class MessageOut(BaseModel):
    role: str
    content: str
    handled_by: str | None = None  # the department, for work; private messages have none
    dashboards: list[dict] = Field(default_factory=list)


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
        made = list(dashboards.get() or []) if result.outcome == "ok" else []
        execution.record_private(subject, body.thread_id, key, body.message, reply, result.department, made)
        _remember_thread(session, key, subject.id, body.thread_id, body.message)
        return mode, ChatOut(
            thread_id=body.thread_id, reply=reply, mode=mode, outcome=result.outcome, route="work",
            department=result.department, approval_id=result.approval_id, task_id=result.task_id,
            dashboards=made,
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
    from app import inflight

    try:
        running = inflight.start(subject.id, body.thread_id, body.message)
    except inflight.Busy as e:
        raise HTTPException(status.HTTP_409_CONFLICT, "Still working on your last message in this chat.") from e
    ok = False
    try:
        out = _chat_now(body, subject, key, session)
        ok = True
        return out
    finally:
        inflight.finish(running, ok)


def _chat_now(body: ChatIn, subject: Subject, key: bytes, session: Session) -> ChatOut:
    handover: dict = {}
    browser_request.set(handover)  # this request's context only
    made: list = []
    dashboards.set(made)
    mode, finished = _before_the_model(body, subject, key, session)
    if finished is not None:
        return finished.model_copy(update={"browser": handover or None})

    context = attachments.context_for_thread(session, key, subject.id, body.thread_id)
    files = attachments.files_for_thread(session, key, subject.id, body.thread_id)
    release_connection(session)
    with _private_slot(subject) as free:
        if not free:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, BUSY_MESSAGE)
        reply = execution.run(subject, body.message, body.thread_id, mode, None, chat_key=key,
                              attachments=context, files=files)
    _remember_thread(session, key, subject.id, body.thread_id, body.message)
    return _private_reply(body.thread_id, mode, reply).model_copy(update={"browser": handover or None, "dashboards": made})


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# An SSE comment: readers ignore it, but it keeps proxies from closing a quiet stream.
PING = ": ping\n\n"


def _next(source: queue.Queue):
    """The next event, or None after KEEPALIVE_SECONDS of silence."""
    try:
        return source.get(timeout=execution.KEEPALIVE_SECONDS)
    except queue.Empty:
        return None


def _stream_response(events: Iterator[str]) -> StreamingResponse:
    # X-Accel-Buffering stops proxies holding the words back until the reply ends.
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class _Refused(Exception):
    """A refusal to show the person as it is (e.g. too many replies at once)."""


FIRST_WAIT = 10  # seconds to wait for a refusal with its own HTTP status before the stream starts


def _run_and_relay(subject: Subject, thread_id: str, message: str, kind: str, work) -> StreamingResponse:
    """Run work(handover, made, emit) in its own thread and relay what it does as server-sent
    events. The work goes on to the end, and is saved, whatever the browser does; its steps and
    outcome are kept in app/inflight.py for a page that comes back. One run per chat at a time.

    Events: {"type":"run","id"} first, then "step"/"delta", then "done" (work's return value) or
    "error". A refusal raised in the first FIRST_WAIT seconds keeps its HTTP status."""
    from app import inflight

    try:
        running = inflight.start(subject.id, thread_id, message, kind)
    except inflight.Busy as e:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Still working on your last message in this chat. Its reply will appear here; "
                            "wait for it, or start a new chat.") from e
    handover: dict = {}  # filled in by the worker: a browser hand-over, dashboards it made
    made: list = []
    events_q: queue.Queue = queue.Queue()

    def emit(kind_: str, value) -> None:
        events_q.put((kind_, value))

    def run() -> None:
        def on_step(text: str) -> None:
            emit("step", text)
            inflight.step(running, text)

        progress_sink.set(on_step)
        browser_request.set(handover)  # this thread's own context (a copy): nothing leaks to the caller
        dashboards.set(made)
        ok = False
        try:
            emit("done", work(handover, made, emit))
            ok = True
        except Exception as e:  # noqa: BLE001 - re-raised or reported below
            emit("fail", e)
        finally:
            inflight.finish(running, ok)

    threading.Thread(target=contextvars.copy_context().run, args=(run,), daemon=True).start()
    try:
        first = events_q.get(timeout=FIRST_WAIT)
    except queue.Empty:
        first = None  # still sorting the message: start the stream now so no proxy gives up on it
    if first is not None and first[0] == "fail" and isinstance(first[1], HTTPException):
        raise first[1]  # nothing sent yet: a refusal keeps its HTTP status

    def events() -> Iterator[str]:
        yield PING  # headers and a first byte now
        yield _event({"type": "run", "id": running.id})
        event = first
        while True:
            if event is None or event[0] == "sorted":
                yield PING
            elif event[0] == "step":
                yield _event({"type": "step", "text": event[1]})
            elif event[0] == "delta":
                yield _event({"type": "delta", "text": event[1]})
            elif event[0] == "fail":
                if isinstance(event[1], _Refused):
                    yield _event({"type": "error", "detail": str(event[1])})
                else:
                    # The type only: an exception's text could quote the message.
                    logger.error("chat stream failed: %s", type(event[1]).__name__)
                    yield _event({"type": "error", "detail": "That request failed on the server. Try again."})
                return
            elif event[0] == "done":
                yield _event({"type": "done", **event[1]})
                return
            event = _next(events_q)

    return _stream_response(events())


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
    # The whole request runs in its own thread, start to finish, whether or not anyone is still
    # reading: the response only relays what it does (see _run_and_relay).

    def work(handover: dict, made: list, emit) -> dict:
        with Session(bind=bind) as own:
            mode, finished = _before_the_model(body, subject, key, own)
        emit("sorted", None)  # the response can start now
        if finished is not None:
            return {**finished.model_dump(), "browser": handover or None}
        with Session(bind=bind) as own:
            _remember_thread(own, key, subject.id, body.thread_id, body.message)  # listed from now on
            context = attachments.context_for_thread(own, key, subject.id, body.thread_id)
            files = attachments.files_for_thread(own, key, subject.id, body.thread_id)
        with _private_slot(subject) as free:
            if not free:
                raise _Refused(BUSY_MESSAGE)
            token_sink.set(lambda text: emit("delta", text))
            reply = execution.run(subject, body.message, body.thread_id, mode, None, chat_key=key,
                                  attachments=context, files=files)
        with Session(bind=bind) as later:
            _remember_thread(later, key, subject.id, body.thread_id, body.message)
        return {**_private_reply(body.thread_id, mode, reply).model_dump(), "browser": handover or None,
                "dashboards": made}

    bind = session.get_bind()
    release_connection(session)
    from app.llm.client import token_sink

    return _run_and_relay(subject, body.thread_id, body.message, "message", work)


def _departments_of(subject: Subject) -> dict[str, str]:
    """The departments this person can work in, for triage: only those they belong to (the
    admin role is its own department, not a pass into the others)."""
    roles = set(subject.roles)
    return {d.name: d.description for d in DEFAULT_DEPARTMENTS if d.name in roles}


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


class RunningOut(BaseModel):
    id: str
    kind: str
    message: str
    steps: list[str]
    seconds: int


class RunStatusOut(BaseModel):
    state: Literal["running", "done", "failed", "unknown"]


@router.get("/runs/{run_id}", response_model=RunStatusOut)
def run_status(run_id: str, subject: Subject = Depends(current_subject)) -> RunStatusOut:
    """How a run ended: done (its reply is saved in the chat), failed, or unknown (lost in a server
    restart: send the message again). Only the person's own runs."""
    from app import inflight

    return RunStatusOut(state=inflight.status(subject.id, run_id))


@router.get("/running", response_model=list[str])
def running_threads(subject: Subject = Depends(current_subject)) -> list[str]:
    """The person's chats with a request still being worked on."""
    from app import inflight

    return inflight.threads(subject.id)


@router.get("/threads/{thread_id}/running", response_model=RunningOut | None)
def thread_running(thread_id: str, subject: Subject = Depends(current_subject)) -> RunningOut | None:
    """The request still being worked on in this chat (question and steps so far), or null."""
    import time

    from app import inflight

    run = inflight.get(subject.id, thread_id)
    if run is None or run.done:
        return None
    return RunningOut(id=run.id, kind=run.kind, message=run.message, steps=list(run.steps),
                      seconds=int(time.time() - run.started))


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
    session: Session = Depends(get_session),
) -> ChatOut:
    """Go ahead with (or decline) the action the assistant asked you to confirm."""
    from app import inflight

    pending = execution.private_pending(subject, thread_id, key)
    if pending is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "nothing is waiting for your confirmation in this chat")
    try:
        running = inflight.start(subject.id, thread_id, "", "confirm")
    except inflight.Busy as e:
        raise HTTPException(status.HTTP_409_CONFLICT, "Already working on this confirmation.") from e
    handover: dict = {}
    browser_request.set(handover)
    made: list = []
    dashboards.set(made)
    ok = False
    try:
        out = _confirm_now(thread_id, body, subject, key, session, pending, made)
        ok = True
    except _Refused as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(e)) from e
    finally:
        inflight.finish(running, ok)
    return out.model_copy(update={"browser": handover or None, "dashboards": made})


@router.post("/threads/{thread_id}/confirm/stream")
def thread_confirm_stream(
    thread_id: str,
    body: ConfirmIn,
    subject: Subject = Depends(current_subject),
    key: bytes = Depends(chat_key),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """Like .../confirm, as server-sent events (the steps of the work it lets go on), running to the
    end whatever the browser does: going on after a confirmation (e.g. a checkout) can take minutes."""
    pending = execution.private_pending(subject, thread_id, key)
    if pending is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "nothing is waiting for your confirmation in this chat")
    bind = session.get_bind()
    release_connection(session)

    def work(handover: dict, made: list, emit) -> dict:
        with Session(bind=bind) as own:
            out = _confirm_now(thread_id, body, subject, key, own, pending, made)
        return {**out.model_dump(), "browser": handover or None, "dashboards": made}

    return _run_and_relay(subject, thread_id, "", "confirm", work)


def _confirm_now(thread_id: str, body: ConfirmIn, subject: Subject, key: bytes, session: Session,
                 pending: dict, made: list) -> ChatOut:
    if body.as_work and pending["tool"] == "browser_commit":
        # Work purchases are paid with company details by Accounts, never from personal ones.
        args = pending["args"]
        guards.forward_to_account(
            session, kind="order", requester_id=subject.id, chat_thread=thread_id, mode="agents",
            thread_id=f"{thread_id}~order-{uuid.uuid4().hex[:6]}",
            message=f"Order for work, prepared in a chat: {args.get('summary', '')}".strip(),
        )
        reason = ("the person said this order is for work: it was sent to the Accounts team, who will place it "
                  "with the company's details. Tell them it's with Accounts and they can follow it in this chat")
        body = ConfirmIn(approve=False)
    else:
        reason = "" if body.approve else "the person said no"
    release_connection(session)  # the model call below can take a while
    with _private_slot(subject) as free:
        if not free:
            raise _Refused(BUSY_MESSAGE)
        try:
            out = execution.resume_private(
                subject, thread_id, key, approved=body.approve, tool=pending["tool"], args=pending["args"],
                mode=pending["mode"], reason=reason,
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
