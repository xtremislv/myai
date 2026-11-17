"""The assistant on WhatsApp: incoming messages, replies, notifications and approvals.

A message from a linked number goes the same way as one typed in the app: guards.triage sorts it (private or
work), work goes through the Request Manager (department tools, quotas, approvals, audit), and private messages
run the agent with WHATSAPP_PRIVATE_TOOLS (context.channel = "whatsapp": no person-present tools, and the
person's own key is not used, see links.py). One conversation per person ("/reset" starts a new one).

Replies: short ones as WhatsApp text; long answers, tables and dashboards on the person's result page, with
the opening of the answer and the link in WhatsApp (format.py).

Out: every bell notification (service.notify) also goes to a linked number: as text inside WhatsApp's 24-hour
window, as the approved template outside it (cloud.Config.template). Approval requests reach linked admins with
Approve / Reject buttons; the result of approved work reaches a requester who asked on WhatsApp.

Meta's webhook must be answered at once: routes_whatsapp hands the payload to handle_payload, which queues each
new message (duplicates from Meta's retries are skipped) and returns.
"""
from __future__ import annotations

import contextvars
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy.orm import Session

from app.acl.models import Subject
from app.db.session import SessionLocal, User
from app.whatsapp import cloud, links
from app.whatsapp import format as fmt

logger = logging.getLogger(__name__)

EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="whatsapp")
LINK_RE = re.compile(r"^\s*link\s+(\d{6})\s*$", re.I)
APPROVAL_BUTTON = re.compile(r"ap:([0-9a-f]{32}):([yn])")
SEEN_SECONDS = 2 * 86400
STRANGER_SECONDS = 6 * 3600

HELP = ("This is TW-ai, the company assistant. Ask anything: research, products and prices, reminders, "
        "work requests for your departments.\n\n"
        "Commands:\n/reset - start a new conversation\n/page - the link to your result page\n"
        "/mute - stop notifications here\n/unmute - turn them back on\n/help - this message\n\n"
        "Your own files, mailbox and saved details stay in the app.")
NOT_LINKED = ("This number isn't linked to a TW-ai account. Sign in to TW-ai, open Settings > WhatsApp, "
              "enter this number and send the code it shows here.")
ONLY_TEXT = "I can read text messages here. For files, images and voice notes, use the TW-ai app."
FAILED = "That request failed on the server. Try again in a moment."


# ---- one-time keys (Redis, shared by the gateway's workers; memory if Redis is down) ----
_memory: dict[str, float] = {}
_memory_lock = threading.Lock()


def _once(key: str, seconds: int) -> bool:
    """True the first time `key` is claimed within `seconds`."""
    from app.llm.model_manager import ledger

    r = ledger._r()
    if r is not None:
        try:
            return bool(r.set(key, "1", nx=True, ex=seconds))
        except Exception:  # noqa: BLE001 - fall back to this process's memory
            pass
    now = time.monotonic()
    with _memory_lock:
        for k in [k for k, until in _memory.items() if until < now]:
            del _memory[k]
        if key in _memory:
            return False
        _memory[key] = now + seconds
        return True


def _submit(fn, *args) -> None:
    """Each task in a fresh context: worker threads are reused, and context vars (channel, user_key) must not
    carry over from one person's message to the next."""
    EXECUTOR.submit(contextvars.Context().run, _guarded, fn, *args)


def _guarded(fn, *args) -> None:
    try:
        fn(*args)
    except Exception as e:  # noqa: BLE001 - the type only: the text could quote the message
        logger.error("whatsapp task failed: %s", type(e).__name__)


def _send(cfg: cloud.Config, phone: str, body: str) -> bool:
    try:
        cloud.send_text(cfg, phone, body)
        return True
    except (cloud.CloudError, Exception) as e:  # noqa: BLE001
        logger.warning("whatsapp send failed: %s %s", type(e).__name__, getattr(e, "code", ""))
        return False


# ---- in ----
def handle_payload(payload: dict[str, Any]) -> int:
    """Queue the new messages in a webhook call. Returns how many were queued."""
    queued = 0
    for message in cloud.incoming(payload):
        if _once(f"wa:seen:{message['id']}", SEEN_SECONDS):
            _submit(handle_message, message)
            queued += 1
    return queued


def _text_of(message: dict) -> tuple[str, str]:
    """(text, button id) of an incoming message."""
    kind = message.get("type")
    if kind == "text":
        return (message.get("text") or {}).get("body", ""), ""
    if kind == "interactive":
        reply = (message.get("interactive") or {}).get("button_reply") or {}
        return reply.get("title", ""), reply.get("id", "")
    if kind == "button":  # a quick-reply button on a template
        return (message.get("button") or {}).get("text", ""), ""
    return "", ""


def handle_message(message: dict, *, runner=None) -> None:
    phone = message["from"]
    text, button = _text_of(message)
    with SessionLocal() as session:
        cfg = cloud.config(session)
        if cfg is None or (message.get("phone_id") and message["phone_id"] != cfg.phone_id):
            return
        link = links.by_phone(session, phone)
        if link is None:
            _stranger(session, cfg, phone, text)
            return
        user = session.get(User, link.user_id)
        if user is None or user.disabled:
            return
        links.touch(session, link)
        cloud.mark_read(cfg, message["id"])
        if button:
            _button(session, cfg, phone, user, button)
            return
        if not text.strip():
            _send(cfg, phone, ONLY_TEXT)
            return
        if _command(session, cfg, phone, link, user, text):
            return
        user_id, thread = user.id, links.thread_id(link)
    answer(cfg, user_id, thread, phone, text.strip(), runner=runner)


def _stranger(session: Session, cfg: cloud.Config, phone: str, text: str) -> None:
    match = LINK_RE.match(text or "")
    if match:
        row = links.confirm(session, phone, match.group(1))
        if row is not None:
            user = session.get(User, row.user_id)
            _send(cfg, phone, f"Linked to your TW-ai account ({user.username if user else row.user_id}).\n\n{HELP}")
        elif _once(f"wa:badcode:{phone}", 60):
            _send(cfg, phone, "That code didn't match, or it expired. Get a new one in TW-ai under Settings > WhatsApp.")
        return
    if _once(f"wa:stranger:{phone}", STRANGER_SECONDS):
        _send(cfg, phone, NOT_LINKED)


def _command(session: Session, cfg: cloud.Config, phone: str, link, user: User, text: str) -> bool:
    cmd = text.strip().lower()
    if cmd in ("/reset", "/new"):
        link.thread_version += 1
        session.commit()
        _send(cfg, phone, "New conversation started.")
    elif cmd in ("/help", "help", "/start"):
        _send(cfg, phone, HELP)
    elif cmd == "/page":
        url = links.page_url(session, link) if links.base_url() else ""
        _send(cfg, phone, f"Your result page: {url}" if url else "Result pages aren't set up on this server yet.")
    elif cmd in ("/mute", "/stop"):
        link.notify = False
        session.commit()
        _send(cfg, phone, "Notifications here are off. They stay on in the app. Send /unmute to turn them back on.")
    elif cmd == "/unmute":
        link.notify = True
        session.commit()
        _send(cfg, phone, "Notifications here are on again.")
    elif LINK_RE.match(text):
        _send(cfg, phone, f"This number is already linked to {user.username}.")
    else:
        return False
    return True


def answer(cfg: cloud.Config, user_id: str, thread: str, phone: str, text: str, *, runner=None) -> None:
    """Triage and run one message like the app's chat box does, then reply."""
    from app import inflight
    from app.api import routes_chat
    from app.orchestrator import execution
    from app.orchestrator.context import channel, dashboards

    with SessionLocal() as session:
        user = session.get(User, user_id)
        subject = Subject(id=user.id, roles=tuple(user.roles or ()))
        key = links.chat_key(session, user_id)
    channel.set("whatsapp")
    made: list = []
    dashboards.set(made)
    try:
        running = inflight.start(user_id, thread, text, "whatsapp")
    except inflight.Busy:
        _send(cfg, phone, "Still working on your last message. Send this again once I've answered it.")
        return
    ok = False
    try:
        with SessionLocal() as session:
            mode, finished = routes_chat._before_the_model(
                routes_chat.ChatIn(message=text, thread_id=thread), subject, key, session, remember=False)
        if finished is None:
            with routes_chat._private_slot(subject) as free:
                if not free:
                    _send(cfg, phone, routes_chat.BUSY_MESSAGE)
                    ok = True
                    return
                run = runner or execution.run
                result = run(subject, text, thread, mode, None, chat_key=key)
            out = routes_chat._private_reply(thread, mode, result)
        else:
            out = finished
        ok = True
    except Exception as e:  # noqa: BLE001 - the type only: the text could quote the message
        logger.error("whatsapp answer failed: %s", type(e).__name__)
        _send(cfg, phone, FAILED)
        return
    finally:
        inflight.finish(running, ok)
    deliver(cfg, user_id, phone, _reply_text(out), list(out.dashboards or made))


def _reply_text(out) -> str:
    if out.outcome == "flagged_nsfw":
        return "I can't help with that. As the company's rules require, this message was reported to HR and the admin."
    if out.outcome == "confirm" and out.confirm is not None:
        return f"This needs your go-ahead in the TW-ai app ({out.confirm.title}): {links.base_url()}/ui/"
    return out.reply or "Done."


def deliver(cfg: cloud.Config, user_id: str, phone: str, text: str, dashboards: list | None = None,
            title: str = "Your answer") -> None:
    """Send an answer: as text if it fits, else its opening plus the person's result page."""
    with SessionLocal() as session:
        link = links.get(session, user_id)
        if link is not None and fmt.needs_page(text, dashboards) and links.base_url():
            url = links.save_page(session, link, fmt.title_of(text, title), text, dashboards or [])
            what = "Full answer and charts" if dashboards else "Full answer"
            _send(cfg, phone, f"{fmt.preview(text)}\n\n{what}: {url}")
            return
    for part in fmt.chunks(fmt.to_whatsapp(text)):
        _send(cfg, phone, part)


# ---- approvals by button ----
def _button(session: Session, cfg: cloud.Config, phone: str, user: User, button: str) -> None:
    match = APPROVAL_BUTTON.fullmatch(button)
    if not match:
        _send(cfg, phone, "That button has expired.")
        return
    from app.request_manager import approvals as approvals_store
    from app.request_manager.router import resolve_approval

    if "admin" not in (user.roles or []):
        _send(cfg, phone, "Only the admin can approve requests.")
        return
    approve = match.group(2) == "y"
    row, won = approvals_store.decide(session, match.group(1), approve=approve, decided_by=user.id,
                                      reason="decided on WhatsApp")
    if row is None:
        _send(cfg, phone, "That request no longer exists.")
        return
    if not won:
        _send(cfg, phone, f"Already {row.state}.")
        return
    _send(cfg, phone, "Approved. Running it now." if approve else "Rejected.")
    result = resolve_approval(session, row)
    if approve:
        _send(cfg, phone, {"ok": "Done.", "pending_approval": "It needs another approval (sent to you)."}.get(
            result.outcome, f"It didn't finish: {result.reason[:300]}" if result.reason else "It didn't finish."))


def approval_requested(session: Session, approval) -> None:
    """A work request waits for approval: Approve / Reject buttons to each linked admin."""
    try:
        admins = [u for u in session.query(User).all() if "admin" in (u.roles or []) and not u.disabled]
        rows = [(u, links.get(session, u.id)) for u in admins]
    except Exception:  # noqa: BLE001 - never fail the request over this
        return
    targets = [(u.id, row.phone) for u, row in rows if row is not None and row.phone]
    if not targets:
        return
    requester = session.get(User, approval.subject_id)
    args = ", ".join(f"{k}: {str(v)[:80]}" for k, v in (approval.tool_args or {}).items() if v not in ("", None))
    body = (f"*Approval needed* ({approval.department})\nFrom: {requester.username if requester else approval.subject_id}\n"
            f"Action: {approval.tool}" + (f"\n{args[:500]}" if args else "") + f"\n\nRequest: {approval.message[:300]}")
    _submit(_send_approval, session.get_bind(), approval.id, f"{approval.tool} for {approval.department}", body, targets)


def _send_approval(bind, approval_id: str, short: str, body: str, targets: list[tuple[str, str]]) -> None:
    with Session(bind=bind) as session:
        cfg = cloud.config(session)
        if cfg is None:
            return
        for user_id, phone in targets:
            row = links.get(session, user_id)
            if row is None or row.phone != phone:
                continue
            if links.in_window(row):
                try:
                    cloud.send_buttons(cfg, phone, body, [(f"ap:{approval_id}:y", "Approve"), (f"ap:{approval_id}:n", "Reject")])
                    continue
                except cloud.CloudError as e:
                    if e.code != cloud.OUTSIDE_WINDOW:
                        logger.warning("whatsapp approval message failed: %s", e.code)
                        continue
            _template(cfg, phone, f"Approval needed: {short}")


def work_result(session: Session, approval, text: str) -> None:
    """Approved (or rejected) work someone asked for on WhatsApp: tell them how it ended."""
    if not links.is_whatsapp_thread(getattr(approval, "chat_thread", None)):
        return
    try:
        row = links.get(session, approval.subject_id)
    except Exception:  # noqa: BLE001
        return
    if row is None or not row.phone:
        return
    head = {"approved": "Your request was approved.", "denied": "Your request was not approved."}.get(
        approval.state, f"Your request ended: {approval.state}.")
    reason = f" Reason: {approval.reason}" if approval.state == "denied" and approval.reason else ""
    _submit(_tell, session.get_bind(), approval.subject_id, row.phone, f"{head}{reason}\n\n{text}".strip())


def _tell(bind, user_id: str, phone: str, text: str) -> None:
    with Session(bind=bind) as session:
        cfg = cloud.config(session)
        row = links.get(session, user_id)
    if cfg is None or row is None or row.phone != phone:
        return
    if links.in_window(row):
        deliver(cfg, user_id, phone, text, title="Your request")
    else:
        _template(cfg, phone, "An update on your request")


# ---- notifications out ----
KIND_LABELS = {"reminder": "Reminder", "alert": "Alert", "scout": "New finds", "job": "Finished",
               "share": "Shared with you", "digest": "Mail digest"}


def notify(session: Session, owner_id: str, kind: str, title: str, body: str = "") -> None:
    """Called for every bell notification (service.notify). Cheap when the person has no linked number."""
    try:
        row = links.get(session, owner_id)
    except Exception:  # noqa: BLE001 - never fail a notification over WhatsApp
        return
    if row is None or not row.phone or not row.notify:
        return
    _submit(_notify, session.get_bind(), owner_id, row.phone, kind, title, body)


def _notify(bind, owner_id: str, phone: str, kind: str, title: str, body: str) -> None:
    with Session(bind=bind) as session:
        cfg = cloud.config(session)
        row = links.get(session, owner_id)
        if cfg is None or row is None or row.phone != phone:
            return
        inside = links.in_window(row)
    label = KIND_LABELS.get(kind, "")
    if inside:
        text = f"*{label + ': ' if label else ''}{title}*" + (f"\n\n{body}" if body else "")
        if fmt.needs_page(body) and links.base_url():
            deliver(cfg, owner_id, phone, f"# {title}\n\n{body}", title=title)
            return
        try:
            cloud.send_text(cfg, phone, fmt.to_whatsapp(text))
            return
        except cloud.CloudError as e:
            if e.code != cloud.OUTSIDE_WINDOW:
                logger.warning("whatsapp notification failed: %s", e.code)
                return
    _template(cfg, phone, f"{label}: {title}" if label else title)


def _template(cfg: cloud.Config, phone: str, text: str) -> None:
    """Outside the 24-hour window only an approved template may be sent (charged per message by Meta)."""
    if not cfg.template:
        return
    try:
        cloud.send_template(cfg, phone, [text])
    except (cloud.CloudError, Exception) as e:  # noqa: BLE001
        logger.warning("whatsapp template failed: %s %s", type(e).__name__, getattr(e, "code", ""))
