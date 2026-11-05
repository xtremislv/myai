"""Finding every email on a topic: all folders, all pages, words matched separately."""
from __future__ import annotations

import datetime as dt

import pytest

from app.mail import connector
from app.mail.connector import MailConfig


class FakeIMAP:
    def __init__(self, gmail=True, n=130):
        self.capabilities = ("IMAP4REV1", "X-GM-EXT-1") if gmail else ("IMAP4REV1",)
        self.n = n
        self.searches = []
        self.selected = None
        self.literal = None

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"']

    def select(self, folder, readonly=False):
        self.selected = folder
        return "OK", [b""]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            self.searches.append(args)
            return "OK", [b" ".join(str(i).encode() for i in range(1, self.n + 1))]
        if cmd == "FETCH":
            ids = args[0].split(b",") if isinstance(args[0], bytes) else [args[0].encode()]
            rows = [(b"1 (UID " + i + b" FLAGS (\\Seen))", b"Subject: Invoice " + i + b"\r\nFrom: a@b.c\r\n\r\n")
                    for i in ids]
            return "OK", rows
        raise AssertionError(cmd)

    def logout(self):
        pass


CFG = MailConfig(imap_host="imap.gmail.com", username="me@gmail.com", password="x")


@pytest.fixture
def imap(monkeypatch):
    box = {}

    def make(**kw):
        box["conn"] = FakeIMAP(**kw)
        monkeypatch.setattr(connector, "_imap", lambda cfg: box["conn"])
        return box["conn"]
    return make


def test_a_gmail_search_covers_every_folder_and_says_how_many_there_are(imap):
    conn = imap()
    out = connector.list_messages(CFG, "", 50, False, "acme invoice", 1)
    assert conn.selected == '"[Gmail]/All Mail"'
    assert conn.searches[-1] == ("ALL", "X-GM-RAW", '"acme invoice"')
    lines = out.splitlines()
    assert lines[0].startswith('130 emails matching "acme invoice" in every folder') and "page=2" in lines[0]
    assert len(lines) == 51 and lines[1].startswith("id all-130")  # newest first


def test_the_last_page_ends_the_list(imap):
    imap()
    out = connector.list_messages(CFG, "", 50, False, "acme", 3)
    assert "showing 101-130" in out and "page=4" not in out
    assert "No more" in connector.list_messages(CFG, "", 50, False, "acme", 4)


def test_other_servers_match_each_word_anywhere(imap):
    conn = imap(gmail=False)
    connector.list_messages(CFG, "", 50, False, 'acme "purchase order" 2026', 1)
    assert conn.searches[-1] == ("ALL", "TEXT", '"acme"', "TEXT", '"purchase order"', "TEXT", '"2026"')


def test_an_archive_id_opens_from_the_archive_folder(imap):
    conn = imap()
    assert connector._target(conn, "all-42", "inbox") == ("[Gmail]/All Mail", "42")
    assert connector._target(conn, "42", "inbox") == ("INBOX", "42")


def test_a_topic_summary_reads_the_matching_mail(imap):
    conn = imap(n=60)
    got = connector.fetch_messages(CFG, "", 100, False, query="acme")
    assert len(got) == connector.MAX_SUMMARY and got[0]["id"] == "all-60"
    assert conn.searches[-1] == ("ALL", "X-GM-RAW", '"acme"')


# --- "How many emails did I get in the last N hours?" (2026-11-05: the agent answered "none"
# because summarize_mail only counts UNREAD mail and no tool could filter by arrival time).

class TimedIMAP(FakeIMAP):
    """Ten emails: UID i arrived i*30 minutes before `now`; UIDs 1-5 already read."""

    def __init__(self, now, gmail=True):
        super().__init__(gmail=gmail, n=10)
        self.now = now

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            self.searches.append(args)
            unseen = "UNSEEN" in args
            ids = [i for i in range(10, 0, -1) if not (unseen and i <= 5)]  # oldest first
            return "OK", [b" ".join(str(i).encode() for i in ids)]
        if cmd == "FETCH" and args[1] == "(UID INTERNALDATE)":
            rows = []
            for i in args[0].split(b","):
                when = (self.now - dt.timedelta(minutes=30 * int(i))).strftime("%d-%b-%Y %H:%M:%S +0000")
                rows.append(b"1 (UID " + i + b' INTERNALDATE "' + when.encode() + b'")')
            return "OK", rows
        return super().uid(cmd, *args)



def test_hours_counts_read_and_unread_mail_that_arrived_in_the_window(imap, monkeypatch):
    now = dt.datetime.now(dt.UTC)
    conn = TimedIMAP(now)
    monkeypatch.setattr(connector, "_imap", lambda cfg: conn)
    out = connector.list_messages(CFG, "inbox", 50, False, "", 1, hours=2)
    # 30, 60, 90 min ago are in; 120 min is on the edge (the clock moved on), 150+ are out.
    assert out.startswith("3 emails received in the last 2 hours in every folder"), out
    search = conn.searches[-1]
    assert "SINCE" in search and search[-1] == f'"{connector.NOT_RECEIVED}"'  # Gmail: received, not sent
    assert conn.selected == '"[Gmail]/All Mail"'


def test_hours_on_other_servers_reads_the_inbox(imap, monkeypatch):
    conn = TimedIMAP(dt.datetime.now(dt.UTC), gmail=False)
    monkeypatch.setattr(connector, "_imap", lambda cfg: conn)
    out = connector.list_messages(CFG, "inbox", 50, False, "", 1, hours=24)
    assert out.startswith("10 emails received in the last day in the inbox folder"), out


def test_an_empty_unread_listing_says_read_mail_may_exist(imap, monkeypatch):
    conn = TimedIMAP(dt.datetime.now(dt.UTC))
    monkeypatch.setattr(connector, "_imap", lambda cfg: conn)
    out = connector.list_messages(CFG, "inbox", 50, True, "", 1, hours=2)
    assert out.startswith("No emails unread received") and "read ones may exist" in out


def test_summaries_in_a_window_include_read_mail(imap, monkeypatch):
    conn = TimedIMAP(dt.datetime.now(dt.UTC))
    monkeypatch.setattr(connector, "_imap", lambda cfg: conn)
    got = connector.fetch_messages(CFG, "inbox", 40, False, hours=2)
    assert [m["id"] for m in got] == ["all-1", "all-2", "all-3"]


def test_window_labels():
    assert connector.window_label(24) == "in the last day"
    assert connector.window_label(48) == "in the last 2 days"
    assert connector.window_label(1) == "in the last 1 hour"
    assert connector.window_label(1.5) == "in the last 1.5 hours"
    assert connector.since_hours(0) is None and connector.since_hours("x") is None


def test_summarize_mail_never_reports_an_empty_mailbox_from_unread_only(monkeypatch):
    from app.orchestrator import tools

    calls = []
    monkeypatch.setattr(tools, "_mailbox", lambda *a: (CFG, None))
    monkeypatch.setattr(connector, "fetch_messages", lambda *a, **kw: calls.append((a, kw)) or [])
    out = tools.summarize_mail({}, category="contact")
    assert "No UNREAD messages" in out and "does NOT mean" in out and "read_mail with hours" in out
    tools.summarize_mail({}, category="contact", hours=3)  # a window includes read mail
    assert calls[-1][0][3] is False and calls[-1][1]["hours"] == 3
