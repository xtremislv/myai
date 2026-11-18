"""Fixes from the 2026-11-18 security audit, one test group per fix."""
from __future__ import annotations

import imaplib
import socket
import threading

import pytest

from app.mail import connector
from app.mail.connector import MailConfig


# ---- 1. IMAP command injection: no control characters reach a command line ----
class _Server:
    """A pre-authenticated IMAP server on localhost that records every command line it gets."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.lines: list[bytes] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        conn.settimeout(3)
        conn.sendall(b"* PREAUTH ready\r\n")
        buf = b""
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for line in lines:
                    self.lines.append(line)
                    tag = line.split(b" ")[0]
                    if b"CAPABILITY" in line:
                        conn.sendall(b"* CAPABILITY IMAP4rev1 X-GM-EXT-1\r\n" + tag + b" OK done\r\n")
                    elif b" LIST " in line:
                        conn.sendall(b'* LIST (\\All) "/" "[Gmail]/All Mail"\r\n' + tag + b" OK done\r\n")
                    elif b"SEARCH" in line:
                        conn.sendall(b"* SEARCH\r\n" + tag + b" OK done\r\n")
                    else:
                        conn.sendall(tag + b" OK done\r\n")
        except OSError:
            return


@pytest.fixture
def imap_server(monkeypatch):
    server = _Server()
    monkeypatch.setattr(connector, "_pool", {})
    monkeypatch.setattr(connector, "_imap", lambda cfg: imaplib.IMAP4("127.0.0.1", server.sock.getsockname()[1]))
    return server


CFG = MailConfig(imap_host="imap.example.com", username="me@example.com", password="pw")
EVIL = "x\r\nA1 DELETE Important\r\nA2 NOOP"


def test_a_folder_with_a_line_break_is_refused_before_anything_is_sent(imap_server):
    for folder in (EVIL, "Inbox\x00"):
        with pytest.raises(ValueError, match="control characters"):
            connector.list_messages(CFG, folder, 10)
    with pytest.raises(ValueError):
        connector.read_messages(CFG, ["5"], EVIL)
    with pytest.raises(ValueError):
        connector.set_flag(CFG, "5", "read", EVIL)
    assert not any(b"DELETE" in line or b"EXAMINE" in line or b"SELECT" in line for line in imap_server.lines)


def test_a_search_with_line_breaks_is_one_search(imap_server):
    connector.list_messages(CFG, "", 10, False, "invoice\r\nA1 DELETE Important\r\nA2 NOOP")
    assert not any(line.startswith(b"A1 ") for line in imap_server.lines)
    [search] = [line for line in imap_server.lines if b"SEARCH" in line]
    assert b'X-GM-RAW "invoice A1 DELETE Important A2 NOOP"' in search


def test_folder_names_are_always_quoted_and_escaped():
    assert connector._mailbox_arg("INBOX") == '"INBOX"'
    assert connector._mailbox_arg('[Gmail]/All Mail') == '"[Gmail]/All Mail"'
    assert connector._mailbox_arg('a"b\\c') == '"a\\"b\\\\c"'
    with pytest.raises(ValueError):
        connector._mailbox_arg("a\nb")


def test_the_mail_tools_answer_with_an_error_instead(imap_server, monkeypatch):
    from app.orchestrator import tools

    monkeypatch.setattr(tools, "_mailbox", lambda *a: (CFG, ""))
    out = tools.read_mail({}, folder=EVIL)
    assert "ERROR" in out and "control characters" in out
    assert not any(b"DELETE" in line for line in imap_server.lines)


# ---- 2. /metrics listens on localhost only ----
def test_metrics_listen_on_localhost_by_default():
    from app.config import Settings
    from app.observability import start_metrics_server

    assert Settings().metrics_host == "127.0.0.1"
    server = start_metrics_server(port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.shutdown()
        server.server_close()


# ---- 5. no quadratic regex on WhatsApp replies; messages have a length cap ----
def test_whatsapp_links_stay_fast_on_a_run_of_brackets():
    import time

    from app.whatsapp import format as fmt

    start = time.perf_counter()
    fmt.to_whatsapp("[" * 60_000 + " and [a link](https://example.com/x)")
    assert time.perf_counter() - start < 1.0
    assert "a link: https://example.com/x" in fmt.to_whatsapp("See [a link](https://example.com/x).")


def test_a_chat_message_has_a_length_cap():
    from pydantic import ValidationError

    from app.api.routes_chat import MAX_MESSAGE_CHARS, ChatIn
    from app.api.routes_request import RequestIn

    ChatIn(message="x" * MAX_MESSAGE_CHARS, thread_id="t")
    with pytest.raises(ValidationError):
        ChatIn(message="x" * (MAX_MESSAGE_CHARS + 1), thread_id="t")
    with pytest.raises(ValidationError):
        RequestIn(message="x" * (MAX_MESSAGE_CHARS + 1), thread_id="t")


# ---- 7. API answers are never cached; tokens only use shared-secret algorithms ----
def test_api_answers_say_no_store_and_routes_keep_their_own():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    assert client.get("/v1/auth/me").headers["cache-control"] == "no-store"         # 401, still no-store
    assert client.get("/r/not-a-real-token").headers["cache-control"] == "no-store"  # was overwritten to no-cache
    assert client.get("/ui/").headers["cache-control"] == "no-cache"
    assert "cache-control" not in client.get("/healthz").headers


def test_only_hmac_jwt_algorithms_are_allowed(monkeypatch):
    import jwt as pyjwt

    from app.auth import security
    from app.config import Settings, secret_problems

    good = Settings(jwt_secret="x" * 40)
    assert not [p for p in secret_problems(good) if "JWT_ALGORITHM" in p]
    for alg in ("none", "RS256", "ES256"):
        assert [p for p in secret_problems(Settings(jwt_secret="x" * 40, jwt_algorithm=alg)) if "JWT_ALGORITHM" in p]
    token = security.issue_token("u", "u", [])
    monkeypatch.setattr(security._settings, "jwt_algorithm", "none")
    with pytest.raises(pyjwt.InvalidAlgorithmError):
        security.decode_token(token)
    forged = pyjwt.encode({"sub": "root", "roles": ["admin"]}, key=None, algorithm="none")
    monkeypatch.setattr(security._settings, "jwt_algorithm", "HS256")
    with pytest.raises(pyjwt.InvalidTokenError):
        security.decode_token(forged)
