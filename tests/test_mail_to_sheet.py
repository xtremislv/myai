"""Every matching email into a sheet (2026-11-04: the agent put 50 of 1,006 in a CSV and called it
complete). Big exports run in the background and notify the person."""
from __future__ import annotations

import io
import json
import re
import time

import pytest
from openpyxl import load_workbook

from app import jobs
from app.mail import connector, export
from app.mail.connector import MailConfig
from app.orchestrator import tools as tools_mod

N = 1006


class FakeIMAP:
    capabilities = ("IMAP4REV1", "X-GM-EXT-1")

    def __init__(self):
        self.searches = []

    def list(self):
        return "OK", [b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"']

    def select(self, folder, readonly=False):
        return "OK", [b""]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            self.searches.append(args)
            return "OK", [b" ".join(str(i).encode() for i in range(1, N + 1))]
        ids = args[0].split(b",")
        return "OK", [(b"1 (UID " + i + b" FLAGS ())",
                       b"From: Ana <ana@x.com>\r\nSubject: AI news " + i + b"\r\nDate: Mon, 3 Nov 2026 10:00:00 +0000\r\n"
                       b"Message-ID: <" + i + b"@x>\r\n\r\nBody about AI " + i) for i in ids]

    def logout(self):
        pass


CFG = MailConfig(imap_host="imap.gmail.com", username="me@gmail.com", password="x")


def fake_chat(messages, **kw):
    ids = re.findall(r'<email id="([^"]+)"', messages[1]["content"])
    return json.dumps([{"id": i, "gist": f"gist {i}", "action": "", "kind": "newsletter"} for i in ids])


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(connector, "_imap", lambda cfg: FakeIMAP())
    monkeypatch.setattr(tools_mod, "_enforce", lambda *a: None)
    monkeypatch.setattr(tools_mod, "_mailbox", lambda state, c, f, a: (CFG, ""))
    monkeypatch.setattr(export.summary.llm, "chat", fake_chat)
    saved, notes = {}, []
    monkeypatch.setattr(tools_mod, "_save", lambda state, path, data, **kw: saved.update({path: data}) or f"Created '{path}' (1 KB).")
    monkeypatch.setattr(jobs, "_notify", lambda owner, title, body: notes.append((owner, title, body)))
    jobs._jobs.clear()
    return saved, notes


def _rows(data):
    return list(load_workbook(io.BytesIO(data))["Data"].iter_rows(values_only=True))


def test_search_all_fetches_every_match_in_chunks_on_one_connection(env):
    total, mails = connector.search_all(CFG, "", "subject:ai", 5000)
    assert total == N and len(mails) == N and mails[0]["id"] == f"all-{N}"
    assert mails[0]["from_email"] == "ana@x.com"


def test_a_big_summarized_export_runs_in_the_background_with_every_email(env):
    saved, notes = env
    out = tools_mod.mail_to_sheet({"subject_id": "u1"}, "subject:ai")
    assert "STARTED IN THE BACKGROUND" in out and "1006 emails match" in out and "Do NOT say" in out
    for _ in range(200):
        if notes:
            break
        time.sleep(0.05)
    assert notes and notes[0][0] == "u1" and notes[0][1].startswith("Ready:")
    rows = _rows(saved["Summaries/Emails - ai.xlsx"])
    assert rows[0][:5] == ("Date", "From", "From email", "Subject", "Summary")
    assert len(rows) == N + 1 and rows[1][4] == f"gist all-{N}"  # all of them, summarized, newest first
    assert "1006 of 1006 emails" in notes[0][2]
    assert "done" in tools_mod.job_status({"subject_id": "u1"})


def test_headers_only_is_done_at_once(env):
    saved, _ = env
    out = tools_mod.mail_to_sheet({"subject_id": "u1"}, "subject:ai", summarize=False, save_as="ai list")
    assert "DONE." in out and "1006 of 1006" in out
    assert len(_rows(saved["ai list.xlsx"])) == N + 1


def test_one_job_at_a_time_per_person(env, monkeypatch):
    monkeypatch.setattr(export, "build", lambda *a, **k: time.sleep(0.5) or (N, ["Date"], []))
    tools_mod.mail_to_sheet({"subject_id": "u2"}, "subject:ai")
    assert "not started" in tools_mod.mail_to_sheet({"subject_id": "u2"}, "subject:ai")


def test_other_servers_understand_subject_and_from():
    assert connector._criterion("subject:ai") == ("SUBJECT", "ai")
    assert connector._criterion("from:bank.com") == ("FROM", "bank.com")
    assert connector._criterion("invoice") == ("TEXT", "invoice")
