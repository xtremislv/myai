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
import hashlib
import imaplib
import json
import re
import smtplib
import ssl
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr

from app.browsing import extract
from app.mail import imapparse

TIMEOUT = 60  # seconds per network read: a big mailbox search or message can take a while
# Emails are read by their structure (BODYSTRUCTURE): the whole text part is fetched, never the
# attachments, which are listed by name and fetched one at a time when asked for (read_attachment).
TEXT_MAX_BYTES = 2 * 1024 * 1024  # of one email's text part (only generated newsletters get near it)
SUMMARY_BODY_CHARS = 20_000  # of each email's text given to a summary (~5k tokens)
ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024  # as stored on the server (base64 is ~4/3 of the file)
# A server whose BODYSTRUCTURE can't be read gets the first this-many bytes of the raw email instead.
SUMMARY_FETCH_BYTES = 256 * 1024
# A signed-in IMAP connection is kept this long after use, so the next call (the agent's next tool
# round, or the next message) skips TLS and the login. Servers drop idle sessions after ~30 minutes.
IDLE_SECONDS = 120
POOL_PER_MAILBOX = 2
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


_pool: dict[tuple, list[tuple[imaplib.IMAP4, float]]] = {}
_pool_lock = threading.Lock()


def _pool_key(cfg: MailConfig) -> tuple:
    # The credentials are part of the key: a changed password or new sign-in never reuses the old session.
    secret = cfg.password or json.dumps(cfg.oauth or {}, sort_keys=True, default=str)
    return (cfg.imap_host, cfg.imap_port, cfg.username, cfg.oauth_provider,
            hashlib.sha256(secret.encode()).hexdigest())


def _close(conn) -> None:
    try:
        conn.logout()
    except Exception:  # noqa: BLE001 - already gone
        try:
            conn.shutdown()
        except Exception:  # noqa: BLE001
            pass


@contextmanager
def _session(cfg: MailConfig) -> Iterator[imaplib.IMAP4]:
    """A signed-in connection: an idle one from the last two minutes when it still answers, else a
    new one. Given back after a clean run, closed after an error (its state is unknown)."""
    key, now = _pool_key(cfg), time.monotonic()
    conn, stale = None, []
    with _pool_lock:
        for k in list(_pool):
            fresh = [(c, t) for c, t in _pool[k] if now - t < IDLE_SECONDS]
            stale += [c for c, t in _pool[k] if now - t >= IDLE_SECONDS]
            if fresh:
                _pool[k] = fresh
            else:
                del _pool[k]
        if _pool.get(key):
            conn = _pool[key].pop()[0]
    for old in stale:
        _close(old)
    if conn is not None:
        try:
            if conn.noop()[0] != "OK":
                raise imaplib.IMAP4.error("noop")
        except Exception:  # noqa: BLE001 - dropped by the server: sign in again
            _close(conn)
            conn = None
    if conn is None:
        conn = _imap(cfg)
    try:
        yield conn
    except BaseException:
        _close(conn)
        raise
    if not isinstance(conn, imaplib.IMAP4):  # a test double: never kept between calls
        _close(conn)
        return
    with _pool_lock:
        idle = _pool.setdefault(key, [])
        extra = conn if len(idle) >= POOL_PER_MAILBOX else None
        if extra is None:
            idle.append((conn, time.monotonic()))
    if extra is not None:
        _close(extra)


def _fetched(rows) -> Iterator[tuple[bytes, bytes, bytes]]:
    """(uid, response items, message bytes) per email of a FETCH reply. Servers may put UID or FLAGS
    after the message literal, in the plain bytes element that follows it."""
    pending: list = []

    def done():
        if pending:
            meta, raw, tail = pending
            uid = re.search(rb"UID (\d+)", meta + b" " + tail)
            if uid:
                yield uid.group(1), meta + b" " + tail, raw

    for row in rows or []:
        if isinstance(row, tuple) and len(row) >= 2:
            yield from done()
            pending = [row[0], row[1], b""]
        elif isinstance(row, bytes) and pending:
            pending[2] += row
    yield from done()


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
            _login(server, cfg.username, cfg.password)
    except Exception:
        server.close()
        raise
    return server


def _login(server: smtplib.SMTP, username: str, password: str) -> None:
    """Sign in with AUTH PLAIN when offered. smtplib's login() tries the next method after a
    rejected password, and Gmail hangs up on that, so a wrong password surfaced as "Connection
    unexpectedly closed" instead of the server's own 535 answer."""
    server.ehlo_or_helo_if_needed()
    if "plain" not in server.esmtp_features.get("auth", "").lower().split():
        server.login(username, password)
        return
    server.user, server.password = username, password
    code, resp = server.auth("PLAIN", server.auth_plain, initial_response_ok=True)
    if code not in (235, 503):
        raise smtplib.SMTPAuthenticationError(code, resp)


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


_INTERNALDATE = re.compile(rb'INTERNALDATE "([^"]+)"')
NOT_RECEIVED = "-in:sent -in:drafts"  # Gmail: a time window counts mail that came in, not mail sent


def since_hours(hours: float) -> dt.datetime | None:
    """The moment `hours` ago (UTC), or None for no time limit."""
    try:
        hours = float(hours or 0)
    except (TypeError, ValueError):
        return None
    return dt.datetime.now(dt.UTC) - dt.timedelta(hours=min(hours, 24 * 366)) if hours > 0 else None


def _received_after(conn, base: list[str], query: str, after: dt.datetime) -> list[bytes]:
    """UIDs (oldest first) of emails the server received at or after `after`. IMAP's SINCE only
    knows whole days (in the server's own time zone), so the search starts a day early and the
    exact arrival time (INTERNALDATE) of each candidate decides."""
    day = (after - dt.timedelta(days=1)).strftime("%d-%b-%Y")
    uids = _search(conn, [*base, "SINCE", day], query)
    kept = []
    for start in range(0, len(uids), 500):
        chunk = uids[start:start + 500]
        _, rows = conn.uid("FETCH", b",".join(chunk), "(UID INTERNALDATE)")
        arrived = {}
        for row in rows or []:
            line = row[0] if isinstance(row, tuple) else row
            if not isinstance(line, bytes):
                continue
            uid, when = re.search(rb"UID (\d+)", line), _INTERNALDATE.search(line)
            if uid and when:
                try:
                    arrived[uid.group(1)] = dt.datetime.strptime(when.group(1).decode().strip(), "%d-%b-%Y %H:%M:%S %z")
                except ValueError:
                    arrived[uid.group(1)] = None
        # An email whose arrival time can't be read is kept: missing mail is worse than one extra.
        kept += [u for u in chunk if arrived.get(u) is None or arrived[u] >= after]
    return kept


def window_label(hours: float) -> str:
    hours = float(hours)
    if hours % 24 == 0:
        days = int(hours // 24)
        return "in the last day" if days == 1 else f"in the last {days} days"
    shown = f"{hours:g}"
    return f"in the last {shown} hour{'' if shown == '1' else 's'}"


def list_messages(cfg: MailConfig, box: str = "inbox", limit: int = 10, unread_only: bool = False,
                  query: str = "", page: int = 1, hours: float = 0) -> str:
    """Emails newest first, one page at a time, with how many there are in all. A search with
    no folder given looks through every folder (the archive folder) when the server has one.
    hours > 0: only emails received in that many past hours (exact count, every folder on Gmail)."""
    after = since_hours(hours)
    with _session(cfg) as conn:
        folder = _folder(box)
        prefix = ""
        gmail = "X-GM-EXT-1" in getattr(conn, "capabilities", ())
        if ((query or (after and gmail)) and (box or "inbox").strip().lower() in ("", "inbox", "all")
                and (archive := _all_mail(conn))):
            folder, prefix = archive, ALL_PREFIX
        status, _ = conn.select(_quote(folder) if " " in folder else folder, readonly=True)
        if status != "OK":
            return f"ERROR: no mailbox folder named '{box}'"
        base = ["UNSEEN"] if unread_only else ["ALL"]
        if after:
            raw = f"{query} {NOT_RECEIVED}".strip() if prefix and gmail else query
            uids = _received_after(conn, base, raw, after)[::-1]
        else:
            uids = _search(conn, base, query)[::-1]
        total = len(uids)
        size = max(1, min(int(limit), MAX_LIST))
        page = max(1, int(page))
        shown = uids[(page - 1) * size:page * size]
        where = "every folder" if prefix else f"the {box or 'inbox'} folder"
        what = " ".join(filter(None, [
            "unread" if unread_only else "", f'matching "{query}"' if query else "",
            f"received {window_label(hours)}" if after else "",
        ]))
        if not shown:
            if page == 1:
                none = f"No emails {what} in {where}."
                if unread_only:
                    none += " (Only UNREAD emails were counted: read ones may exist; call again with unread_only false.)"
                return none
            return f"No more: all {total} emails were shown."
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
    with body_bytes 0 only the headers are fetched (fast), else each email's whole text (no attachments)."""
    with _session(cfg) as conn:
        _, prefix = _select(conn, box, query)
        uids = _search(conn, ["ALL"], query)[::-1]
        total = len(uids)
        wanted = uids[:max(0, limit)]
        if body_bytes:
            found = _with_text(conn, wanted, prefix, SUMMARY_BODY_CHARS, progress)
            return total, [found[u] for u in wanted if u in found]
        part = "BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)]"
        found = {}
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


_HEAD = "FROM TO CC SUBJECT DATE MESSAGE-ID"


def _item_bytes(item: dict, prefix: str) -> bytes | None:
    """The literal of the first reply item whose name starts with prefix ("BODY[HEADER", "BODY[1.2]")."""
    for key, val in item.items():
        if key.startswith(prefix.upper()) and isinstance(val, bytes):
            return bytes(val)
    return None


def _structure(conn, uids: list[bytes]) -> dict[bytes, dict]:
    """uid -> {"head": header Message, "unread", "parts", "body_part"}: one small FETCH per 50 emails."""
    out: dict[bytes, dict] = {}
    for start in range(0, len(uids), FETCH_CHUNK):
        chunk = uids[start:start + FETCH_CHUNK]
        _, rows = conn.uid("FETCH", b",".join(chunk), f"(UID FLAGS BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS ({_HEAD})])")
        for item in imapparse.messages(rows):
            uid = str(item.get("UID") or "")
            if not uid.isdigit():
                continue
            parts = imapparse.parts(item.get("BODYSTRUCTURE"))
            flags = item.get("FLAGS") if isinstance(item.get("FLAGS"), list) else []
            out[uid.encode()] = {
                "head": email.message_from_bytes(_item_bytes(item, "BODY[HEADER") or b"", policy=email.policy.default),
                "unread": "\\Seen" not in [str(f) for f in flags],
                "parts": parts, "body_part": imapparse.body_part(parts),
            }
    return out


def _texts(conn, info: dict[bytes, dict]) -> dict[bytes, str]:
    """uid -> the email's whole text, fetching only each email's text part: one FETCH per part
    number (most emails share "1" or "1.1"), never the attachments."""
    groups: dict[str, list[bytes]] = {}
    for uid, i in info.items():
        if i["body_part"] is not None:
            groups.setdefault(i["body_part"].section, []).append(uid)
    out: dict[bytes, str] = {}
    for section, uids in groups.items():
        for start in range(0, len(uids), FETCH_CHUNK):
            chunk = uids[start:start + FETCH_CHUNK]
            _, rows = conn.uid("FETCH", b",".join(chunk), f"(UID BODY.PEEK[{section}]<0.{TEXT_MAX_BYTES}>)")
            for item in imapparse.messages(rows):
                uid = str(item.get("UID") or "").encode()
                data = _item_bytes(item, f"BODY[{section}]")
                if uid not in info or data is None:
                    continue
                part = info[uid]["body_part"]
                try:
                    text = imapparse.text_of(data, part)
                    if part.type == "text/html":
                        text = extract.page_text(text)[1]
                except Exception:  # noqa: BLE001 - an odd encoding must not stop the rest
                    text = ""
                if part.size > TEXT_MAX_BYTES:
                    text += f"\n[the rest of this very long email was not fetched: {imapparse.size_label(part.size)}]"
                out[uid] = text
    return out


def _files(parts: list) -> list[str]:
    return [p.filename for p in parts if p.attachment and p.filename]


def _with_text(conn, uids: list[bytes], prefix: str, body_chars: int, progress=None) -> dict[bytes, dict]:
    """The summary dicts of these emails with their whole text (clipped to body_chars, spaces
    collapsed) and every attachment's name. Emails whose structure the server didn't give fall
    back to the first SUMMARY_FETCH_BYTES of the raw email."""
    info = _structure(conn, uids)
    texts = _texts(conn, info)
    out: dict[bytes, dict] = {}
    for uid, i in info.items():
        head = i["head"]
        sender = _header(head, "From")
        out[uid] = {
            "id": prefix + uid.decode(), "message_id": _header(head, "Message-ID"), "from": sender,
            "from_email": parseaddr(sender)[1], "subject": _header(head, "Subject") or "(no subject)",
            "date": _header(head, "Date"), "unread": i["unread"], "attachments": _files(i["parts"]),
            "body": extract.clip(" ".join(texts.get(uid, "").split()), body_chars),
        }
    missing = [u for u in uids if u not in out or (not _has_structure(info.get(u)))]
    for start in range(0, len(missing), FETCH_CHUNK):
        chunk = missing[start:start + FETCH_CHUNK]
        _, rows = conn.uid("FETCH", b",".join(chunk), f"(UID FLAGS BODY.PEEK[]<0.{SUMMARY_FETCH_BYTES}>)")
        for uid, meta, raw in _fetched(rows):
            mail = _summary_of(prefix + uid.decode(), meta, raw, True)
            mail["body"] = extract.clip(mail["body"], body_chars)
            out[uid] = mail
    if progress:
        progress(len(out), len(uids))
    return out


def _has_structure(i: dict | None) -> bool:
    """False when the structure gave nothing to read: no parts at all (a server that sent no
    BODYSTRUCTURE). An email with only attachments has parts, and no text, which is right."""
    return bool(i and i["parts"])


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


MAX_OPEN = 10  # emails one open_mail call reads


def _open_text(head, text: str, parts: list, max_chars: int, part: int = 1) -> str:
    lines = "\n".join(f"{h}: {_header(head, h)}" for h in ("From", "To", "Cc", "Date", "Subject", "Message-ID")
                      if head.get(h))
    files = [p for p in parts if p.attachment and p.filename]
    if files:
        # Sizes as the person knows them: base64 on the server is ~4/3 of the file.
        sized = [f"{p.filename} ({imapparse.size_label(p.size * 3 // 4 if p.encoding == 'base64' else p.size)})"
                 for p in files]
        lines += f"\nAttachments: {', '.join(sized)} (open one with open_mail attachment=<name>)"
    text = text.strip()
    pages = max(1, -(-len(text) // max_chars))
    if pages == 1:
        return lines + "\n\n" + text
    part = min(max(1, int(part)), pages)
    piece = text[(part - 1) * max_chars:part * max_chars]
    more = (f"[part {part} of {pages}: call open_mail with part={part + 1} for the next part]" if part < pages
            else f"[part {part} of {pages}: the end of the email]")
    return lines + "\n\n" + (f"[...part {part}, continuing]\n" if part > 1 else "") + piece + "\n" + more


def _ids_by_folder(conn, ids: list[str], box: str) -> tuple[dict[str, list[tuple[str, str]]], dict[str, str]]:
    archive = _all_mail(conn) if any(i.lower().startswith(ALL_PREFIX) for i in ids) else None
    by_folder: dict[str, list[tuple[str, str]]] = {}
    errors: dict[str, str] = {}
    for mail_id in ids:
        try:
            if mail_id.lower().startswith(ALL_PREFIX):
                folder, uid = archive or _folder(box), str(int(mail_id[len(ALL_PREFIX):]))
            else:
                folder, uid = _folder(box), str(int(mail_id))
        except ValueError:
            errors[mail_id] = f"ERROR: '{mail_id}' is not an email id"
            continue
        by_folder.setdefault(folder, []).append((mail_id, uid))
    return by_folder, errors


def _open_raw(conn, uids: list[str], max_chars: int) -> dict[str, str]:
    """Fallback for a server that gave no structure: the whole raw email, parsed here."""
    if not uids:
        return {}
    _, rows = conn.uid("FETCH", ",".join(uids), "(UID BODY.PEEK[])")
    raw_of = {uid.decode(): raw for uid, _, raw in _fetched(rows)}
    if len(uids) == 1 and not raw_of:  # a server that leaves UID out of a one-email reply
        raw_of = {uids[0]: next((r[1] for r in rows or [] if isinstance(r, tuple)), None)}
    out = {}
    for uid, raw in raw_of.items():
        if raw is None:
            continue
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        out[uid] = _format_open(msg, max_chars)
    return out


def _format_open(msg, max_chars: int) -> str:
    files = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
    head = "\n".join(
        f"{h}: {_header(msg, h)}" for h in ("From", "To", "Cc", "Date", "Subject", "Message-ID") if msg.get(h)
    )
    return (
        head + (f"\nAttachments: {', '.join(files)}" if files else "")
        + "\n\n" + extract.clip(_body(msg).strip(), max_chars)
    )


def read_message(cfg: MailConfig, message_id: str, box: str = "inbox", max_chars: int = 60_000, part: int = 1) -> str:
    return read_messages(cfg, [message_id], box, max_chars, part)


def read_messages(cfg: MailConfig, message_ids: list[str], box: str = "inbox", max_chars: int = 60_000,
                  part: int = 1) -> str:
    """Emails in full (the whole text; attachments listed with their sizes, not fetched) on one
    connection. One id: the email alone; several: each under an "=== email <id> ===" line."""
    ids = list(dict.fromkeys(str(m).strip() for m in message_ids if str(m).strip()))[:MAX_OPEN]
    if not ids:
        return "ERROR: no email id given"
    with _session(cfg) as conn:
        by_folder, texts = _ids_by_folder(conn, ids, box)
        for folder, wanted in by_folder.items():
            conn.select(_quote(folder) if " " in folder else folder, readonly=True)
            uids = [uid.encode() for _, uid in wanted]
            info = _structure(conn, uids)
            body = _texts(conn, info)
            raw = _open_raw(conn, [u.decode() for u in uids if not _has_structure(info.get(u))], max_chars)
            for mail_id, uid in wanted:
                i = info.get(uid.encode())
                if _has_structure(i):
                    texts[mail_id] = _open_text(i["head"], body.get(uid.encode(), ""), i["parts"], max_chars, part)
                else:
                    texts[mail_id] = raw.get(uid, f"ERROR: no message with id {mail_id}")
    if len(ids) == 1:
        return texts[ids[0]]
    return "\n\n".join(f"=== email {i} ===\n{texts[i]}" for i in ids)


def _pick(files: list, name: str):
    """The attachment called `name`: exact (any case), else the only one containing it, else by
    number (1 = the first listed)."""
    want = name.strip().casefold()
    exact = [p for p in files if p.filename.casefold() == want]
    if exact:
        return exact[0]
    near = [p for p in files if want and want in p.filename.casefold()]
    if len(near) == 1:
        return near[0]
    if want.isdigit() and 1 <= int(want) <= len(files):
        return files[int(want) - 1]
    return None


def read_attachment(cfg: MailConfig, message_id: str, name: str, box: str = "inbox") -> tuple[str, str, bytes] | str:
    """(file name, content type, the file's bytes) of one attachment, or an ERROR string."""
    with _session(cfg) as conn:
        by_folder, errors = _ids_by_folder(conn, [str(message_id).strip()], box)
        if errors:
            return next(iter(errors.values()))
        folder, [(mail_id, uid)] = next(iter(by_folder.items()))
        conn.select(_quote(folder) if " " in folder else folder, readonly=True)
        info = _structure(conn, [uid.encode()]).get(uid.encode())
        if info is None:
            return f"ERROR: no message with id {mail_id}"
        files = [p for p in info["parts"] if p.attachment and p.filename]
        if not files:
            return f"ERROR: email {mail_id} has no attachments"
        part = _pick(files, name)
        if part is None:
            return (f"ERROR: email {mail_id} has no attachment called '{name}'. Its attachments: "
                    + ", ".join(p.filename for p in files))
        if part.size > ATTACHMENT_MAX_BYTES:
            return f"ERROR: {part.filename} is too big to open here ({imapparse.size_label(part.size)})"
        _, rows = conn.uid("FETCH", uid, f"(UID BODY.PEEK[{part.section}])")
        got = next(iter(imapparse.messages(rows)), {})
        data = _item_bytes(got, f"BODY[{part.section}]")
        if data is None:
            return f"ERROR: the mail server did not send {part.filename}"
        return part.filename, part.type, imapparse.decode(data, part.encoding)


def fetch_messages(
    cfg: MailConfig, box: str = "inbox", limit: int = 10, unread_only: bool = True,
    since: dt.date | None = None, body_chars: int = SUMMARY_BODY_CHARS, query: str = "", hours: float = 0,
) -> list[dict]:
    """The newest messages with their text, without marking them read. Each is
    {"id", "message_id", "from", "subject", "date", "unread", "attachments", "body"}."""
    with _session(cfg) as conn:
        after = since_hours(hours)
        gmail = "X-GM-EXT-1" in getattr(conn, "capabilities", ())
        folder, prefix = _folder(box), ""
        if ((query or (after and gmail)) and (box or "inbox").strip().lower() in ("", "inbox", "all")
                and (archive := _all_mail(conn))):
            folder, prefix = archive, ALL_PREFIX
        status, _ = conn.select(_quote(folder) if " " in folder else folder, readonly=True)
        if status != "OK":
            raise ValueError(f"no mailbox folder named '{box}'")
        criteria = ["UNSEEN"] if unread_only else ["ALL"]
        if since is not None:
            criteria += ["SINCE", since.strftime("%d-%b-%Y")]
        if after:
            raw = f"{query} {NOT_RECEIVED}".strip() if prefix and gmail else query
            found = _received_after(conn, criteria, raw, after)
        else:
            found = _search(conn, criteria, query)
        uids = found[-max(1, min(limit, MAX_SUMMARY)):][::-1]
        # Structure + text parts for all of them in a few FETCH commands (one FETCH per email was a server
        # round trip each: 40-60 s for 40), whole text, no attachments.
        got = _with_text(conn, uids, prefix, body_chars)
        return [got[u] for u in uids if u in got]


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
    with _session(cfg) as conn:
        folder, uid = _target(conn, message_id, box)
        conn.select(_quote(folder) if " " in folder else folder)
        op, flag = flags[action]
        status, _ = conn.uid("STORE", uid, op, flag)
        return f"Marked message {message_id} as {action}." if status == "OK" else f"ERROR: could not mark {message_id}"


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
