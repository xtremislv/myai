"""Long private runs (2026-11-04): a quiet stream must keep talking so proxies don't cut it off,
and a dropped mail connection is retried, not reported as a bad login."""
from __future__ import annotations

import imaplib
import time

from app.mail import connector, summary
from app.orchestrator import execution
from app.orchestrator import tools as tools_mod


def test_a_quiet_run_sends_keepalives_and_still_finishes(monkeypatch):
    import asyncio

    from app.acl.models import Subject
    from app.api import routes_chat

    monkeypatch.setattr(execution, "KEEPALIVE_SECONDS", 0.05)
    monkeypatch.setattr(routes_chat, "FIRST_WAIT", 0.05)

    def slow_work(handover, made, emit):
        time.sleep(0.3)  # a long model call with nothing to show
        return {"reply": "done"}

    response = routes_chat._run_and_relay(Subject(id="u", roles=[]), "quiet", "m", "message", slow_work)

    async def collect():
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(collect())
    assert '"reply": "done"' in chunks[-1]
    assert sum(1 for c in chunks if c == routes_chat.PING) >= 3


def test_the_stream_route_turns_silence_into_sse_comments():
    from app.api import routes_chat

    assert routes_chat.PING.startswith(":") and routes_chat.PING.endswith("\n\n")  # ignored by readers


def test_a_dropped_mail_connection_is_retried_once():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise imaplib.IMAP4.abort("socket error: EOF")
        return "ok"

    assert tools_mod._mail_call(flaky) == "ok" and len(calls) == 2


def test_a_connection_that_keeps_dropping_is_not_called_a_bad_login():
    def broken():
        raise imaplib.IMAP4.abort("socket error: EOF")

    out = tools_mod._mail_call(broken)
    assert "dropped" in out and "reconnect" not in out


def test_sending_is_never_retried(monkeypatch):
    calls = []

    def send(*a):
        calls.append(1)
        raise imaplib.IMAP4.abort("EOF")

    monkeypatch.setattr(connector, "send", send)
    tools_mod._mail_call(connector.send)
    assert len(calls) == 1


def test_a_real_login_refusal_still_says_reconnect():
    def refused():
        raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")

    assert "reconnect" in tools_mod._mail_call(refused)


def test_one_failed_batch_does_not_lose_the_mail_summary():
    mails = [{"id": str(i), "from": "a", "subject": f"s{i}", "date": "", "body": f"body {i}"} for i in range(12)]
    seen = []

    def chat(messages, **kw):
        if 'id="4"' in messages[1]["content"]:
            raise RuntimeError("model hiccup")
        return "[]"

    items = summary.analyse(mails, chat=chat, progress=seen.append)
    assert [i["id"] for i in items] == [str(i) for i in range(12)]  # all kept, in order
    assert seen[-1] == "Read 12 of 12 emails"
