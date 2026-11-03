"""IMAP/SMTP access to one mailbox, configured by a vault secret.

Secret names: ``mail/<category>`` for company boxes (finance, career, contact) and
``mail/personal/<user id>`` for a person's own box. The value is JSON:

    {"imap_host": "imap.gmail.com", "imap_port": 993,
     "smtp_host": "smtp.gmail.com", "smtp_port": 465,
     "username": "accounts@example.com", "password": "<app password>",
     "from": "Accounts <accounts@example.com>"}

smtp_port 587 uses STARTTLS; 465 uses TLS from the start. Messages are addressed by IMAP UID.
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import json
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr

from app.browsing import extract

TIMEOUT = 20
_ADDR = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


@dataclass(frozen=True)
class MailConfig:
    imap_host: str
    username: str
    password: str
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 465
    sender: str = ""

    @classmethod
    def from_secret(cls, raw: str) -> MailConfig:
        data = json.loads(raw)
        return cls(
            imap_host=data["imap_host"], username=data["username"], password=data["password"],
            imap_port=int(data.get("imap_port", 993)), smtp_host=data.get("smtp_host", ""),
            smtp_port=int(data.get("smtp_port", 465)), sender=data.get("from") or data["username"],
        )


def secret_name(category: str, owner: str | None) -> str:
    return f"mail/personal/{owner}" if category == "personal" else f"mail/{category}"


def _imap(cfg: MailConfig) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT)
    conn.login(cfg.username, cfg.password)
    return conn


def _folder(box: str) -> str:
    box = (box or "INBOX").strip()
    return "INBOX" if box.lower() == "inbox" else box


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "").replace('"', "") + '"'


def _header(msg, name: str) -> str:
    return " ".join(str(msg.get(name, "")).split())[:200]


def list_messages(cfg: MailConfig, box: str = "inbox", limit: int = 10, unread_only: bool = False,
                  query: str = "") -> str:
    conn = _imap(cfg)
    try:
        status, _ = conn.select(_folder(box), readonly=True)
        if status != "OK":
            return f"ERROR: no mailbox folder named '{box}'"
        criteria = ["UNSEEN"] if unread_only else ["ALL"]
        if query:
            criteria = [*criteria, "TEXT", _quote(query)]
        _, data = conn.uid("SEARCH", "CHARSET", "UTF-8", *criteria) if query else conn.uid("SEARCH", *criteria)
        uids = (data[0] or b"").split()[-max(1, min(limit, 30)):][::-1]
        if not uids:
            return "No messages."
        _, rows = conn.uid("FETCH", b",".join(uids), "(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        lines = []
        for row in rows:
            if not isinstance(row, tuple):
                continue
            meta, raw = row
            uid = re.search(rb"UID (\d+)", meta)
            unread = b"\\Seen" not in meta
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            lines.append(
                f"id {uid.group(1).decode() if uid else '?'}{' (unread)' if unread else ''}: "
                f"{_header(msg, 'Subject') or '(no subject)'} | from {_header(msg, 'From')} | {_header(msg, 'Date')}"
            )
        return "\n".join(lines)
    finally:
        conn.logout()


def _body(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    if part.get_content_subtype() == "html":
        return extract.page_text(content)[1]
    return content


def read_message(cfg: MailConfig, message_id: str, box: str = "inbox", max_chars: int = 3500) -> str:
    conn = _imap(cfg)
    try:
        conn.select(_folder(box), readonly=True)
        _, rows = conn.uid("FETCH", str(int(message_id)), "(BODY.PEEK[])")
        raw = next((r[1] for r in rows if isinstance(r, tuple)), None)
        if raw is None:
            return f"ERROR: no message with id {message_id}"
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        files = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
        head = "\n".join(
            f"{h}: {_header(msg, h)}" for h in ("From", "To", "Cc", "Date", "Subject", "Message-ID") if msg.get(h)
        )
        return (
            head + (f"\nAttachments: {', '.join(files)}" if files else "")
            + "\n\n" + extract.clip(_body(msg).strip(), max_chars)
        )
    finally:
        conn.logout()


def set_flag(cfg: MailConfig, message_id: str, action: str, box: str = "inbox") -> str:
    flags = {"read": ("+FLAGS", "\\Seen"), "unread": ("-FLAGS", "\\Seen"),
             "flag": ("+FLAGS", "\\Flagged"), "unflag": ("-FLAGS", "\\Flagged")}
    if action not in flags:
        return f"ERROR: action must be one of {', '.join(flags)}"
    conn = _imap(cfg)
    try:
        conn.select(_folder(box))
        op, flag = flags[action]
        status, _ = conn.uid("STORE", str(int(message_id)), op, flag)
        return f"Marked message {message_id} as {action}." if status == "OK" else f"ERROR: could not mark {message_id}"
    finally:
        conn.logout()


def valid_recipients(to: str) -> list[str]:
    addrs = [parseaddr(a)[1] for a in re.split(r"[,;]", to) if a.strip()]
    if not addrs or not all(_ADDR.match(a) for a in addrs):
        raise ValueError(f"'{to}' is not a list of email addresses")
    return addrs


def send(cfg: MailConfig, to: str, subject: str, body: str, in_reply_to: str = "") -> str:
    if not cfg.smtp_host:
        return "ERROR: this mailbox has no smtp_host configured, so it can't send"
    recipients = valid_recipients(to)
    msg = EmailMessage()
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject.replace("\n", " ")[:200]
    msg["Message-ID"] = make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = msg["References"] = in_reply_to.strip()
    msg.set_content(body)
    context = ssl.create_default_context()
    if cfg.smtp_port == 465:
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=context, timeout=TIMEOUT)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=TIMEOUT)
        server.starttls(context=context)
    with server:
        server.login(cfg.username, cfg.password)
        server.send_message(msg)
    return f"Sent to {', '.join(recipients)}: {msg['Subject']}"
