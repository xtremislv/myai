"""Finding every email on a topic: all folders, all pages, words matched separately."""
from __future__ import annotations

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
