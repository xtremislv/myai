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
LINK_RE = re.compile(r"^[\s*_]*link\b[\s:#*_-]*(\d(?:[\s-]?\d){5})[\s*_.]*$", re.I)  # "LINK 123456", "link: 123 456"
APPROVAL_BUTTON = re.compile(r"ap:([0-9a-f]{32}):([yn])")
SEEN_SECONDS = 8 * 86400  # Meta retries undelivered webhooks for up to 7 days
STRANGER_SECONDS = 6 * 3600

HELP = ("This is TW-ai, the company assistant. Ask anything: research, products and prices, reminders, "
        "work requests for your departments.\n\n"
        "Commands:\n/reset - start a new conversation\n/page - the link to your result page\n"
        "/mute - stop notifications here\n/unmute - turn them back on\n/help - this message\n\n"
        "You can read and summarize your mail here; sending mail, your own files and saved details stay in the app.")
NOT_LINKED = ("This number isn't linked to a TW-ai account. Sign in to TW-ai, open Settings > WhatsApp, "
              "enter this number and send the code it shows here.")
ONLY_TEXT = ("I can read text, voice notes and documents (PDF, Word, Excel, text files) here. For images and other "
             "files, use the TW-ai app.")
VOICE_MAX = 12 * 1024 * 1024  # as the app's voice input (routes_voice.MAX_BYTES)
DOC_MAX = 20 * 1024 * 1024
NSFW_SAMPLE_CHARS = 4000
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
        heard = ""
        if message.get("type") in ("audio", "document"):
            got = _media(session, cfg, phone, link, message)
            if got is None:
                return
            text, heard = got
        elif message.get("type") not in ("text", "interactive", "button") or not text.strip():
            _send(cfg, phone, ONLY_TEXT)
            return
        elif _command(session, cfg, phone, link, user, text):
            return
        user_id, thread = user.id, links.thread_id(link)
    answer(cfg, user_id, thread, phone, text.strip(), runner=runner, heard=heard)


def _media(session: Session, cfg: cloud.Config, phone: str, link, message: dict) -> tuple[str, str] | None:
    """A voice note becomes its words (local Whisper, as the app's mic button); a document's text is kept with the
    conversation for links.FILE_DAYS. Returns (text to answer, words heard) or None when there's nothing to answer."""
    from fastapi import HTTPException

    kind = message["type"]
    obj = message.get(kind) or {}
    try:
        data, mime = cloud.download_media(cfg, str(obj.get("id", "")), VOICE_MAX if kind == "audio" else DOC_MAX)
    except cloud.MediaTooLarge:
        _send(cfg, phone, "That file is too large to read here (voice notes up to about 3 minutes, documents up to "
                          "20 MB). Upload it in the TW-ai app instead.")
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("whatsapp media download failed: %s", type(e).__name__)
        _send(cfg, phone, "I couldn't fetch that file from WhatsApp. Try sending it again.")
        return None
    if kind == "audio":
        from app.api import routes_voice

        try:
            routes_voice._throttle(link.user_id)
            heard = str(routes_voice.transcribe(data).get("text") or "").strip()
        except HTTPException as e:
            _send(cfg, phone, "Too many voice notes in a minute: wait a moment." if e.status_code == 429
                  else "Voice notes can't be transcribed right now. Type it instead.")
            return None
        if not heard:
            _send(cfg, phone, "I couldn't make out any words in that voice note.")
            return None
        return heard, heard
    from app.api.routes_chat import safe_filename
    from app.request_manager import guards
    from app.storage.attachments import extract_text

    name = safe_filename(obj.get("filename") or "document")
    text = extract_text(name, mime, data)
    if text is None:
        _send(cfg, phone, f"I can't read {name}: PDF, Word, Excel and text files work.")
        return None
    flagged, _why = guards.is_nsfw(text[:NSFW_SAMPLE_CHARS])
    if flagged:  # as an upload in the app: refused and reported
        guards.report_nsfw(session, link.user_id, f"[WhatsApp document {name}] {text[:2000]}")
        _send(cfg, phone, "I can't read that document. As the company's rules require, it was reported to HR and the admin.")
        return None
    links.add_file(session, link, name, text)
    caption = str(obj.get("caption") or "").strip()
    if not caption:
        _send(cfg, phone, f"Got {name}. Ask me about it: I'll keep it with this conversation for "
                          f"{links.FILE_DAYS} days (/reset clears it).")
        return None
    return caption, ""


def _stranger(session: Session, cfg: cloud.Config, phone: str, text: str) -> None:
    match = LINK_RE.match(text or "")
    if match:
        row = links.confirm(session, phone, re.sub(r"\D", "", match.group(1)))
        if row is not None:
            user = session.get(User, row.user_id)
            _send(cfg, phone, f"Linked to your TW-ai account ({user.username if user else row.user_id}).\n\n{HELP}")
        elif _once(f"wa:badcode:{phone}", 60):
            _send(cfg, phone, "That code didn't match, or it expired. Get a new one in TW-ai under Settings > WhatsApp, and send it "
                  "from the same WhatsApp number you entered there.")
        return
    if _once(f"wa:stranger:{phone}", STRANGER_SECONDS):
        _send(cfg, phone, NOT_LINKED)


def _command(session: Session, cfg: cloud.Config, phone: str, link, user: User, text: str) -> bool:
    cmd = text.strip().lower()
    if cmd in ("/reset", "/new"):
        link.thread_version += 1
        links.clear_files(session, link)
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


def answer(cfg: cloud.Config, user_id: str, thread: str, phone: str, text: str, *, runner=None, heard: str = "") -> None:
    """Triage and run one message like the app's chat box does, then reply."""
    from app import inflight
    from app.api import routes_chat
    from app.orchestrator import execution
    from app.orchestrator.context import channel, dashboards

    with SessionLocal() as session:
        user = session.get(User, user_id)
        subject = Subject(id=user.id, roles=tuple(user.roles or ()))
        key = links.chat_key(session, user_id)
        files = links.files_context(session, links.get(session, user_id))
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
                result = run(subject, text, thread, mode, None, chat_key=key, attachments=files)
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
    note = f'_Heard: "{heard[:300]}{"…" if len(heard) > 300 else ""}"_' if heard else ""
    deliver(cfg, user_id, phone, _reply_text(out), list(out.dashboards or made), note=note)


def _reply_text(out) -> str:
    if out.outcome == "flagged_nsfw":
        return "I can't help with that. As the company's rules require, this message was reported to HR and the admin."
    if out.outcome == "confirm" and out.confirm is not None:
        return f"This needs your go-ahead in the TW-ai app ({out.confirm.title}): {links.base_url()}/ui/"
    return out.reply or "Done."


def deliver(cfg: cloud.Config, user_id: str, phone: str, text: str, dashboards: list | None = None,
            title: str = "Your answer", note: str = "") -> None:
    """Send an answer: as text if it fits, else its opening plus the person's result page. `note` goes first
    (what was heard in a voice note)."""
    head = f"{note}\n\n" if note else ""
    with SessionLocal() as session:
        link = links.get(session, user_id)
        if link is not None and fmt.needs_page(text, dashboards) and links.base_url():
            url = links.save_page(session, link, fmt.title_of(text, title), text, dashboards or [])
            what = "Full answer and charts" if dashboards else "Full answer"
            _send(cfg, phone, f"{head}{fmt.preview(text)}\n\n{what}: {url}")
            return
    for part in fmt.chunks(head + fmt.to_whatsapp(text)):
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
