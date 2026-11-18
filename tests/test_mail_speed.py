"""Mail summaries without one server round trip per email: one FETCH for all, a reused
connection, several emails per open_mail, every summary batch at once, early steps."""
from __future__ import annotations

import imaplib
import threading
import time

import pytest

from app.mail import connector, summary
from app.mail.connector import MailConfig

CFG = MailConfig(imap_host="imap.example.com", username="me@example.com", password="pw")


def _mail(uid: bytes, body: bytes = b"Hello there") -> bytes:
    return (b"From: Ann <ann@x.com>\r\nSubject: Note " + uid + b"\r\nDate: Mon, 16 Nov 2026 10:00:00 +0530\r\n"
            b"Message-ID: <" + uid + b"@x>\r\n\r\n" + body)


LONG = ("Opening line. " + "filler words here " * 8000 + "The crucial part: pay by Friday.").encode()
PDF = b"%PDF-fake"
MIXED = (b'((("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "QUOTED-PRINTABLE" ' + str(len(LONG)).encode()
         + b' 4 NIL NIL NIL NIL)("TEXT" "HTML" ("CHARSET" "utf-8") NIL NIL "7BIT" 300 10 NIL NIL NIL NIL) '
         b'"ALTERNATIVE" ("BOUNDARY" "b2") NIL NIL NIL)("APPLICATION" "PDF" ("NAME" "report.pdf") NIL NIL '
         b'"BASE64" 4000 NIL ("ATTACHMENT" ("FILENAME" "report.pdf")) NIL NIL) "MIXED" ("BOUNDARY" "b1") NIL NIL NIL)')
SINGLE = b'("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "BASE64" 200 3 NIL NIL NIL NIL)'


class FakeIMAP:
    """Odd uids: multipart (long QP text + HTML + report.pdf); even: one base64 text part.
    raw=True: a server that sends no BODYSTRUCTURE (the raw fallback)."""
    capabilities = ("IMAP4REV1",)

    def __init__(self, n=40, uid_after_literal=False, raw=False):
        self.n, self.fetches, self.selected, self.closed = n, [], [], 0
        self.uid_after_literal, self.raw = uid_after_literal, raw

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"']

    def select(self, folder, readonly=False):
        self.selected.append(folder)
        return "OK", [b""]

    def _head(self, i):
        return (b"From: Ann <ann@x.com>\r\nSubject: Note " + i + b"\r\nDate: Mon, 16 Nov 2026 10:00:00 +0530\r\n"
                b"Message-ID: <" + i + b"@x>\r\n\r\n")

    def uid(self, cmd, *args):
        import base64
        import quopri

        if cmd == "SEARCH":
            return "OK", [b" ".join(str(i).encode() for i in range(1, self.n + 1))]
        assert cmd == "FETCH"
        self.fetches.append(args)
        ids = args[0] if isinstance(args[0], bytes) else args[0].encode()
        what, rows = args[1], []
        for i in ids.split(b","):
            odd = int(i) % 2 == 1
            if "BODYSTRUCTURE" in what:
                if self.raw:
                    rows += [b"1 (UID " + i + b" FLAGS (\\Seen))"]
                    continue
                head = self._head(i)
                bs = MIXED if odd else SINGLE
                if self.uid_after_literal:
                    rows += [(b"1 (FLAGS () BODYSTRUCTURE " + bs + b" BODY[HEADER.FIELDS (FROM SUBJECT)] {%d}"
                              % len(head), head), b" UID " + i + b")"]
                else:
                    rows += [(b"1 (UID " + i + b" FLAGS (\\Seen) BODYSTRUCTURE " + bs
                              + b" BODY[HEADER.FIELDS (FROM SUBJECT)] {%d}" % len(head), head), b")"]
            elif "BODY.PEEK[1.1]" in what:
                data = quopri.encodestring(LONG)
                rows += [(b"1 (UID " + i + b" BODY[1.1]<0> {%d}" % len(data), data), b")"]
            elif "BODY.PEEK[1]" in what and "BODY.PEEK[1]<" in what or what == "(UID BODY.PEEK[1])":
                data = base64.encodebytes(b"Short note " + i)
                rows += [(b"1 (UID " + i + b" BODY[1]<0> {%d}" % len(data), data), b")"]
            elif "BODY.PEEK[2]" in what:
                data = base64.encodebytes(PDF)
                rows += [(b"1 (UID " + i + b" BODY[2] {%d}" % len(data), data), b")"]
            else:  # raw email
                rows += [(b"1 (UID " + i + b" FLAGS (\\Seen) BODY[] {9}", _mail(i)), b")"]
        return "OK", rows

    def logout(self):
        self.closed += 1


def test_a_summary_reads_whole_texts_in_a_few_commands_without_attachments(monkeypatch):
    conn = FakeIMAP()
    monkeypatch.setattr(connector, "_imap", lambda cfg: conn)
    got = connector.fetch_messages(CFG, "inbox", 40, False, body_chars=500_000)
    assert len(got) == 40 and len(conn.fetches) == 3  # structure, then part 1.1 and part 1 groups
    assert not any("BODY.PEEK[2]" in f[1] or "BODY.PEEK[]" in f[1] for f in conn.fetches)
    odd, even = got[1], got[0]  # newest first: 40, 39, ...
    assert odd["id"] == "39" and odd["subject"] == "Note 39" and odd["attachments"] == ["report.pdf"]
    assert odd["body"].endswith("The crucial part: pay by Friday.")  # past 64 KB: the whole text
    assert even["body"] == "Short note 40" and even["unread"] is False
    assert conn.closed == 1  # a test double is never pooled
    clipped = connector.fetch_messages(CFG, "inbox", 1, False)
    assert len(clipped[0]["body"]) < connector.SUMMARY_BODY_CHARS + 100  # summaries still get a bounded share


def test_uid_after_the_literal_and_a_server_without_structure(monkeypatch):
    conn = FakeIMAP(n=3, uid_after_literal=True)
    monkeypatch.setattr(connector, "_imap", lambda cfg: conn)
    got = connector.fetch_messages(CFG, "inbox", 10, False)
    assert [m["id"] for m in got] == ["3", "2", "1"] and all(m["unread"] for m in got)
    plain = FakeIMAP(n=2, raw=True)
    monkeypatch.setattr(connector, "_imap", lambda cfg: plain)
    got = connector.fetch_messages(CFG, "inbox", 10, False)
    assert [m["body"] for m in got] == ["Hello there", "Hello there"]


def test_several_emails_open_in_one_call_grouped_by_folder(monkeypatch):
    conn = FakeIMAP()
    monkeypatch.setattr(connector, "_imap", lambda cfg: conn)
    out = connector.read_messages(CFG, ["5", "all-7", "6", "5", "bad"], "inbox")
    assert out.index("=== email 5 ===") < out.index("=== email all-7 ===") < out.index("=== email 6 ===")
    assert "Subject: Note 7" in out and "'bad' is not an email id" in out
    assert "Attachments: report.pdf (3 KB)" in out and "Short note 6" in out
    single = connector.read_message(CFG, "5")
    assert single.startswith("From: Ann") and "=== email" not in single and "part 1 of 3" in single
    last = connector.read_message(CFG, "5", part=3)
    assert last.rstrip().endswith("the end of the email]") and "pay by Friday" in last
    assert "Short note 6" in connector.read_message(CFG, "6") and "part 1 of" not in connector.read_message(CFG, "6")


def test_an_attachment_is_fetched_alone_by_name_or_number(monkeypatch):
    conn = FakeIMAP()
    monkeypatch.setattr(connector, "_imap", lambda cfg: conn)
    assert connector.read_attachment(CFG, "5", "REPORT.pdf") == ("report.pdf", "application/pdf", PDF)
    assert connector.read_attachment(CFG, "5", "1")[0] == "report.pdf"
    assert "no attachment called 'x.doc'" in connector.read_attachment(CFG, "5", "x.doc")
    assert "has no attachments" in connector.read_attachment(CFG, "6", "a")


def test_open_mail_reads_an_attachment_as_text(monkeypatch):
    from app.orchestrator import tools
    from app.storage import attachments

    monkeypatch.setattr(tools, "_mailbox", lambda *a: (CFG, ""))
    monkeypatch.setattr(connector, "read_attachment", lambda cfg, i, name, box: ("a.pdf", "application/pdf", b"x"))
    monkeypatch.setattr(attachments, "extract_text", lambda name, kind, data: "Total due: 500")
    assert "Total due: 500" in tools.open_mail({}, "5", attachment="a.pdf")
    monkeypatch.setattr(attachments, "extract_text", lambda name, kind, data: None)
    monkeypatch.setattr(connector, "read_attachment", lambda cfg, i, name, box: ("p.png", "image/png", b"x" * 2048))
    assert "can't be read as text" in tools.open_mail({}, "5", attachment="p.png")
    fwd = b"From: Bo <b@x.com>\r\nSubject: Old\r\n\r\nForwarded words"
    monkeypatch.setattr(connector, "read_attachment", lambda cfg, i, name, box: ("f.eml", "message/rfc822", fwd))
    assert "Forwarded words" in tools.open_mail({}, "5", attachment="f.eml")


def test_open_mail_tool_splits_ids(monkeypatch):
    from app.orchestrator import tools

    calls = []
    monkeypatch.setattr(tools, "_mailbox", lambda *a: (CFG, ""))
    monkeypatch.setattr(connector, "read_messages", lambda cfg, ids, box, each: calls.append((ids, each)) or "x")
    monkeypatch.setattr(connector, "read_message", lambda cfg, i, box, n, part: calls.append(i) or "y")
    tools.open_mail({}, "12, 15 all-30")
    tools.open_mail({}, "12")
    assert calls[0] == (["12", "15", "all-30"], 26_666) and calls[1] == "12"


class PooledIMAP(imaplib.IMAP4):
    """A real IMAP4 subclass (so it is pooled) with no network."""

    def __init__(self, alive=True):  # noqa: D107 - skip the socket
        self.alive, self.closed = alive, 0

    def noop(self):
        if not self.alive:
            raise imaplib.IMAP4.abort("gone")
        return "OK", [b""]

    def logout(self):
        self.closed += 1


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.setattr(connector, "_pool", {})
    made = []

    def make(cfg):
        made.append(PooledIMAP())
        return made[-1]
    monkeypatch.setattr(connector, "_imap", make)
    return made


def test_a_connection_is_reused_within_two_minutes(pool, monkeypatch):
    with connector._session(CFG):
        pass
    with connector._session(CFG):
        pass
    assert len(pool) == 1 and pool[0].closed == 0
    other = MailConfig(imap_host="imap.example.com", username="me@example.com", password="new")
    with connector._session(other):
        pass
    assert len(pool) == 2  # new credentials, new session
    later = time.monotonic() + connector.IDLE_SECONDS + 1
    monkeypatch.setattr(connector.time, "monotonic", lambda: later)
    with connector._session(CFG):
        pass
    assert len(pool) == 3 and pool[0].closed == 1  # too old: closed, signed in again


def test_a_dead_or_failed_connection_is_not_reused(pool):
    with connector._session(CFG):
        pass
    pool[0].alive = False
    with connector._session(CFG):
        pass
    assert len(pool) == 2 and pool[0].closed == 1
    with pytest.raises(RuntimeError):
        with connector._session(CFG):
            raise RuntimeError("mid-command")
    assert pool[1].closed == 1 and not connector._pool.get(connector._pool_key(CFG))


def test_two_calls_at_once_get_different_connections(pool):
    with connector._session(CFG) as a, connector._session(CFG) as b:
        assert a is not b
    assert len(connector._pool[connector._pool_key(CFG)]) == 2


def test_all_batches_run_at_once_results_keep_order_and_stream_early():
    started, gate = [], threading.Barrier(10, timeout=5)

    def chat(messages, **kw):
        started.append(1)
        gate.wait()  # passes only when all 10 batches are in flight together
        ids = [line.split('"')[1] for line in messages[1]["content"].splitlines() if line.startswith("<email id=")]
        return "[" + ",".join(f'{{"id": "{i}", "gist": "g{i}", "urgent": {"true" if i == "3" else "false"}, '
                              f'"action": "{"pay" if i == "7" else ""}"}}' for i in ids) + "]"

    mails = [{"id": str(n), "from": '"Bob" <bob@x.com>', "subject": f"S{n}", "date": "", "body": "b"} for n in range(40)]
    steps = []
    items = summary.analyse(mails, chat=chat, on_batch=summary.early_notes(steps.append))
    assert [i["id"] for i in items] == [str(n) for n in range(40)]
    assert len(started) == 10
    assert sorted(steps) == ["To do: S7 from Bob: pay", "Urgent: S3 from Bob: g3"]


def test_early_notes_stop_at_the_limit():
    steps = []
    note = summary.early_notes(steps.append, limit=2)
    note([{"urgent": True, "action": "", "gist": "g", "subject": f"s{n}", "from": "a", "deadline": ""} for n in range(5)])
    assert len(steps) == 2
