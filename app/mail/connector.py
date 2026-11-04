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

import datetime as dt
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

TIMEOUT = 60  # seconds per network read: a big mailbox search or message can take a while
# A summary reads at most this much of each email: the text comes first, attachments after it.
SUMMARY_FETCH_BYTES = 256 * 1024
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
    # Set for mailboxes a person configured themselves: the servers must be public internet
    # addresses on mail ports, so nobody can aim the server at its own internal services.
    guarded: bool = False
    # Signed in with OAuth (app/mail/oauth.py): "google" or "microsoft"; then `password` is empty
    # and `oauth` holds the stored tokens. `secret` is the vault name, to save refreshed tokens.
    oauth_provider: str = ""
    oauth: dict | None = None
    secret: str = ""

    @classmethod
    def from_secret(cls, raw: str, *, guarded: bool = False, name: str = "") -> MailConfig:
        data = json.loads(raw)
        oauth = data.get("auth") == "oauth"
        if oauth and not data.get("refresh_token"):
            raise ValueError("the mailbox sign-in is incomplete")
        return cls(
            imap_host=data["imap_host"], username=data["username"], password="" if oauth else data["password"],
            imap_port=int(data.get("imap_port", 993)), smtp_host=data.get("smtp_host", ""),
            smtp_port=int(data.get("smtp_port", 465)), sender=data.get("from") or data["username"],
            guarded=guarded, oauth_provider=data.get("provider", "") if oauth else "",
            oauth=data if oauth else None, secret=name,
        )

    @property
    def method(self) -> str:
        return f"{self.oauth_provider} sign-in" if self.oauth_provider else "password"


IMAP_PORTS = {993}
SMTP_PORTS = {465, 587}


def check_servers(cfg: MailConfig) -> None:
    """Raises ValueError unless the servers are public addresses on mail ports."""
    from app.browsing.netguard import Blocked, resolve_public

    if cfg.imap_port not in IMAP_PORTS:
        raise ValueError("the IMAP port must be 993 (IMAP over TLS)")
    if cfg.smtp_host and cfg.smtp_port not in SMTP_PORTS:
        raise ValueError("the SMTP port must be 465 or 587")
    try:
        resolve_public(cfg.imap_host, cfg.imap_port)
        if cfg.smtp_host:
            resolve_public(cfg.smtp_host, cfg.smtp_port)
    except Blocked as e:
        raise ValueError(str(e)) from None


def secret_name(category: str, owner: str | None) -> str:
    return f"mail/personal/{owner}" if category == "personal" else f"mail/{category}"


def _imap(cfg: MailConfig) -> imaplib.IMAP4_SSL:
    if cfg.guarded:
        check_servers(cfg)
    conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT)
    try:
        if cfg.oauth_provider:
            auth = _xoauth2(cfg)
            conn.authenticate("XOAUTH2", lambda _challenge: auth.encode())
        else:
            conn.login(cfg.username, cfg.password)
    except Exception:
        try:
            conn.shutdown()
        except Exception:  # noqa: BLE001
            pass
        raise
    return conn


def _xoauth2(cfg: MailConfig) -> str:
    from app.mail import oauth

    return oauth.xoauth2(cfg.username, oauth.access_token(cfg.secret, cfg.oauth or {}))


def _smtp(cfg: MailConfig) -> smtplib.SMTP:
    """A signed-in SMTP connection (465: TLS from the start; 587: STARTTLS)."""
    context = ssl.create_default_context()
    if cfg.smtp_port == 465:
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=context, timeout=TIMEOUT)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=TIMEOUT)
        server.starttls(context=context)
    try:
        if cfg.oauth_provider:
            auth = _xoauth2(cfg)
            server.ehlo()
            server.auth("XOAUTH2", lambda challenge=None: auth, initial_response_ok=True)
        else:
            server.login(cfg.username, cfg.password)
    except Exception:
        server.close()
        raise
    return server


def _folder(box: str) -> str:
    box = (box or "INBOX").strip()
    return "INBOX" if box.lower() == "inbox" else box


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "").replace('"', "") + '"'


def _header(msg, name: str) -> str:
    return " ".join(str(msg.get(name, "")).split())[:200]


MAX_LIST = 100  # emails per page of a listing or search
MAX_SUMMARY = 40  # emails read in full by one summary
ALL_PREFIX = "all-"  # ids from the archive folder ("All Mail"): UIDs are per folder
_LIST_ROW = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$')


def _all_mail(conn) -> str | None:
    """The folder holding every email (Gmail's "[Gmail]/All Mail", marked \\All, RFC 6154):
    searching only the inbox misses everything archived."""
    try:
        _, rows = conn.list()
    except Exception:  # noqa: BLE001 - a server without LIST just has no archive folder
        return None
    for row in rows or []:
        line = row.decode(errors="replace") if isinstance(row, bytes) else str(row)
        match = _LIST_ROW.match(line)
        if match and "\\all" in match.group("flags").lower():
            return match.group("name").strip().strip('"')
    return None


def _target(conn, message_id: str, box: str) -> tuple[str, str]:
    """(folder, uid) for an id a listing showed: "all-123" is in the archive folder."""
    message_id = str(message_id).strip()
    if message_id.lower().startswith(ALL_PREFIX):
        return _all_mail(conn) or _folder(box), str(int(message_id[len(ALL_PREFIX):]))
    return _folder(box), str(int(message_id))


def _terms(query: str) -> list[str]:
    """A topic as separate words ("quoted phrases" kept): an email matches when it has them
    all, anywhere, rather than only when it has the exact phrase."""
    return [t.strip('"') for t in re.findall(r'"[^"]+"|\S+', query) if t.strip('"')]


_FIELDS = {"subject": "SUBJECT", "from": "FROM", "to": "TO"}


def _criterion(term: str) -> tuple[str, str]:
    """("SUBJECT", "ai") for "subject:ai" (Gmail-style, understood everywhere); TEXT otherwise."""
    field, _, value = term.partition(":")
    if value and field.lower() in _FIELDS:
        return _FIELDS[field.lower()], value.strip('"')
    return "TEXT", term


def _search(conn, base: list[str], query: str) -> list[bytes]:
    if not query:
        _, data = conn.uid("SEARCH", *base)
        return (data[0] or b"").split()
    if "X-GM-EXT-1" in getattr(conn, "capabilities", ()):
        # Gmail: its own search syntax (words anywhere, from:, subject:, older_than:, ...).
        words = ["X-GM-RAW"]
        value = query
    elif not query.isascii():
        words, value = ["TEXT"], query
    else:
        pairs = [_criterion(t) for t in _terms(query)] or [("TEXT", query)]
        words = [w for key, val in pairs[:-1] for w in (key, _quote(val))] + [pairs[-1][0]]
        value = pairs[-1][1]
    if value.isascii():
        _, data = conn.uid("SEARCH", *base, *words, _quote(value))
    else:  # non-ASCII goes as a literal: imaplib only sends ASCII arguments
        conn.literal = value.encode("utf-8")
        _, data = conn.uid("SEARCH", "CHARSET", "UTF-8", *base, *words)
    return (data[0] or b"").split()


def list_messages(cfg: MailConfig, box: str = "inbox", limit: int = 10, unread_only: bool = False,
                  query: str = "", page: int = 1) -> str:
    """Emails newest first, one page at a time, with how many there are in all. A search with
    no folder given looks through every folder (the archive folder) when the server has one."""
    conn = _imap(cfg)
    try:
        folder = _folder(box)
        prefix = ""
        if query and (box or "inbox").strip().lower() in ("", "inbox", "all") and (archive := _all_mail(conn)):
            folder, prefix = archive, ALL_PREFIX
        status, _ = conn.select(_quote(folder) if " " in folder else folder, readonly=True)
        if status != "OK":
            return f"ERROR: no mailbox folder named '{box}'"
        uids = _search(conn, ["UNSEEN"] if unread_only else ["ALL"], query)[::-1]
        total = len(uids)
        size = max(1, min(int(limit), MAX_LIST))
        page = max(1, int(page))
        shown = uids[(page - 1) * size:page * size]
        where = "every folder" if prefix else f"the {box or 'inbox'} folder"
        what = f'matching "{query}"' if query else ("unread" if unread_only else "")
        if not shown:
            return f"No emails {what} in {where}." if page == 1 else f"No more: all {total} emails were shown."
        _, rows = conn.uid("FETCH", b",".join(shown), "(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        found = {}
        for row in rows:
            if not isinstance(row, tuple):
                continue
            meta, raw = row
            uid = re.search(rb"UID (\d+)", meta)
            if not uid:
                continue
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            unread = " (unread)" if b"\\Seen" not in meta else ""
            found[uid.group(1)] = (
                f"id {prefix}{uid.group(1).decode()}{unread}: "
                f"{_header(msg, 'Subject') or '(no subject)'} | from {_header(msg, 'From')} | {_header(msg, 'Date')}"
            )
        first = (page - 1) * size + 1
        last = first + len(shown) - 1
        head = f"{total} emails {what} in {where}, newest first; showing {first}-{last}.".replace("  ", " ")
        if last < total:
            head += f" {total - last} more: call again with page={page + 1} to see them."
        return "\n".join([head, *(found[u] for u in shown if u in found)])
    finally:
        conn.logout()


def _select(conn, box: str, query: str) -> tuple[str, str]:
    """Open the folder a search should look in: (folder, id prefix). With no folder given, a
    search covers every folder (the archive) when the server has one."""
    folder, prefix = _folder(box), ""
    if query and (box or "inbox").strip().lower() in ("", "inbox", "all") and (archive := _all_mail(conn)):
        folder, prefix = archive, ALL_PREFIX
    status, _ = conn.select(_quote(folder) if " " in folder else folder, readonly=True)
    if status != "OK":
        raise ValueError(f"no mailbox folder named '{box}'")
    return folder, prefix


FETCH_CHUNK = 50  # messages per FETCH command


def search_all(cfg: MailConfig, box: str, query: str, limit: int, body_bytes: int = 0,
               progress=None) -> tuple[int, list[dict]]:
    """(total matches, up to `limit` newest matching emails), on one connection. Each email is
    {"id", "message_id", "from", "from_email", "subject", "date", "unread", "attachments", "body"};
    with body_bytes 0 only the headers are fetched (fast), else the first body_bytes of each."""
    conn = _imap(cfg)
    try:
        _, prefix = _select(conn, box, query)
        uids = _search(conn, ["ALL"], query)[::-1]
        total = len(uids)
        wanted = uids[:max(0, limit)]
        part = f"BODY.PEEK[]<0.{body_bytes}>" if body_bytes else "BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)]"
        found: dict[bytes, dict] = {}
        for start in range(0, len(wanted), FETCH_CHUNK):
            chunk = wanted[start:start + FETCH_CHUNK]
            _, rows = conn.uid("FETCH", b",".join(chunk), f"(UID FLAGS {part})")
            for row in rows:
                if not isinstance(row, tuple):
                    continue
                meta, raw = row
                uid = re.search(rb"UID (\d+)", meta)
                if uid:
                    found[uid.group(1)] = _summary_of(prefix + uid.group(1).decode(), meta, raw, bool(body_bytes))
            if progress:
                progress(min(start + FETCH_CHUNK, len(wanted)), len(wanted))
        return total, [found[u] for u in wanted if u in found]
    finally:
        conn.logout()


def _summary_of(mail_id: str, meta: bytes, raw: bytes, with_body: bool) -> dict:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    sender = _header(msg, "From")
    body, files = "", []
    if with_body:
        try:
            body = " ".join(_body(msg).split())
        except Exception:  # noqa: BLE001 - an odd encoding must not stop the rest
            body = ""
        try:
            files = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
        except Exception:  # noqa: BLE001 - a cut-off message may end mid-attachment
            files = []
    return {
        "id": mail_id, "message_id": _header(msg, "Message-ID"), "from": sender,
        "from_email": parseaddr(sender)[1], "subject": _header(msg, "Subject") or "(no subject)",
        "date": _header(msg, "Date"), "unread": b"\\Seen" not in meta, "attachments": files, "body": body,
    }


def _body(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    if part.get_content_subtype() == "html":
        return extract.page_text(content)[1]
    return content


def read_message(cfg: MailConfig, message_id: str, box: str = "inbox", max_chars: int = 12_000) -> str:
    conn = _imap(cfg)
    try:
        folder, uid = _target(conn, message_id, box)
        conn.select(_quote(folder) if " " in folder else folder, readonly=True)
        _, rows = conn.uid("FETCH", uid, "(BODY.PEEK[])")
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


def fetch_messages(
    cfg: MailConfig, box: str = "inbox", limit: int = 10, unread_only: bool = True,
    since: dt.date | None = None, body_chars: int = 3000, query: str = "",
) -> list[dict]:
    """The newest messages with their text, without marking them read. Each is
    {"id", "message_id", "from", "subject", "date", "unread", "attachments", "body"}."""
    conn = _imap(cfg)
    try:
        folder, prefix = _folder(box), ""
        if query and (box or "inbox").strip().lower() in ("", "inbox", "all") and (archive := _all_mail(conn)):
            folder, prefix = archive, ALL_PREFIX
        status, _ = conn.select(_quote(folder) if " " in folder else folder, readonly=True)
        if status != "OK":
            raise ValueError(f"no mailbox folder named '{box}'")
        criteria = ["UNSEEN"] if unread_only else ["ALL"]
        if since is not None:
            criteria += ["SINCE", since.strftime("%d-%b-%Y")]
        uids = _search(conn, criteria, query)[-max(1, min(limit, MAX_SUMMARY)):][::-1]
        out = []
        for uid in uids:
            _, rows = conn.uid("FETCH", uid, f"(UID FLAGS BODY.PEEK[]<0.{SUMMARY_FETCH_BYTES}>)")
            for row in rows:
                if not isinstance(row, tuple):
                    continue
                meta, raw = row
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                try:
                    body = _body(msg).strip()
                except Exception:  # noqa: BLE001 - an odd encoding must not stop the rest
                    body = ""
                out.append({
                    "id": prefix + uid.decode(),
                    "message_id": _header(msg, "Message-ID"),
                    "from": _header(msg, "From"),
                    "subject": _header(msg, "Subject") or "(no subject)",
                    "date": _header(msg, "Date"),
                    "unread": b"\\Seen" not in meta,
                    "attachments": [p.get_filename() for p in msg.iter_attachments() if p.get_filename()],
                    "body": extract.clip(" ".join(body.split()), body_chars),
                })
                break
        return out
    finally:
        conn.logout()


def check_login(cfg: MailConfig) -> None:
    """Raises if the IMAP login fails (and the SMTP one, when SMTP is set)."""
    _imap(cfg).logout()  # checks the servers first when guarded
    if cfg.smtp_host:
        with _smtp(cfg):
            pass


def set_flag(cfg: MailConfig, message_id: str, action: str, box: str = "inbox") -> str:
    flags = {"read": ("+FLAGS", "\\Seen"), "unread": ("-FLAGS", "\\Seen"),
             "flag": ("+FLAGS", "\\Flagged"), "unflag": ("-FLAGS", "\\Flagged")}
    if action not in flags:
        return f"ERROR: action must be one of {', '.join(flags)}"
    conn = _imap(cfg)
    try:
        folder, uid = _target(conn, message_id, box)
        conn.select(_quote(folder) if " " in folder else folder)
        op, flag = flags[action]
        status, _ = conn.uid("STORE", uid, op, flag)
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
    if cfg.guarded:
        check_servers(cfg)
    msg = EmailMessage()
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject.replace("\n", " ")[:200]
    msg["Message-ID"] = make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = msg["References"] = in_reply_to.strip()
    msg.set_content(body)
    with _smtp(cfg) as server:
        server.send_message(msg)
    return f"Sent to {', '.join(recipients)}: {msg['Subject']}"
