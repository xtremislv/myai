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


class FakeIMAP:
    capabilities = ("IMAP4REV1",)

    def __init__(self, n=40, uid_after_literal=False):
        self.n, self.fetches, self.selected, self.closed = n, [], [], 0
        self.uid_after_literal = uid_after_literal

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"']

    def select(self, folder, readonly=False):
        self.selected.append(folder)
        return "OK", [b""]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return "OK", [b" ".join(str(i).encode() for i in range(1, self.n + 1))]
        assert cmd == "FETCH"
        self.fetches.append(args)
        ids = args[0] if isinstance(args[0], bytes) else args[0].encode()
        rows = []
        for i in ids.split(b","):
            if self.uid_after_literal:
                rows += [(b"1 (FLAGS () BODY[] {9}", _mail(i)), b" UID " + i + b")"]
            else:
                rows += [(b"1 (UID " + i + b" FLAGS (\\Seen) BODY[] {9}", _mail(i)), b")"]
        return "OK", rows

    def logout(self):
        self.closed += 1


def test_a_summary_fetches_all_its_emails_in_one_command(monkeypatch):
    conn = FakeIMAP()
    monkeypatch.setattr(connector, "_imap", lambda cfg: conn)
    got = connector.fetch_messages(CFG, "inbox", 40, False)
    assert len(conn.fetches) == 1 and len(got) == 40
    assert got[0]["id"] == "40" and got[0]["subject"] == "Note 40" and got[0]["unread"] is False
    assert f"<0.{64 * 1024}>" in conn.fetches[0][1]
    assert conn.closed == 1  # a test double is never pooled


def test_uid_after_the_message_literal_is_still_matched(monkeypatch):
    conn = FakeIMAP(n=3, uid_after_literal=True)
    monkeypatch.setattr(connector, "_imap", lambda cfg: conn)
    got = connector.fetch_messages(CFG, "inbox", 10, False)
    assert [m["id"] for m in got] == ["3", "2", "1"] and all(m["unread"] for m in got)


def test_several_emails_open_in_one_call_grouped_by_folder(monkeypatch):
    conn = FakeIMAP()
    monkeypatch.setattr(connector, "_imap", lambda cfg: conn)
    out = connector.read_messages(CFG, ["5", "all-7", "6", "5", "bad"], "inbox")
    assert len(conn.fetches) == 2  # inbox (5, 6) and the archive (7)
    assert out.index("=== email 5 ===") < out.index("=== email all-7 ===") < out.index("=== email 6 ===")
    assert "Subject: Note 7" in out and "'bad' is not an email id" in out
    single = connector.read_message(CFG, "5")
    assert single.startswith("From: Ann") and "===" not in single


def test_open_mail_tool_splits_ids(monkeypatch):
    from app.orchestrator import tools

    calls = []
    monkeypatch.setattr(tools, "_mailbox", lambda *a: (CFG, ""))
    monkeypatch.setattr(connector, "read_messages", lambda cfg, ids, box, each: calls.append((ids, each)) or "x")
    monkeypatch.setattr(connector, "read_message", lambda cfg, i, box: calls.append(i) or "y")
    tools.open_mail({}, "12, 15 all-30")
    tools.open_mail({}, "12")
    assert calls[0] == (["12", "15", "all-30"], 13_333) and calls[1] == "12"


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
