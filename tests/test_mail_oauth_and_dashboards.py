"""Mail sign-in with Google/Microsoft (OAuth + XOAUTH2), dashboards, and the Python sandbox."""
from __future__ import annotations

import base64
import json
import time
import urllib.parse

import pytest

from app import dashboards, sandbox
from app.mail import connector, oauth
from app.orchestrator import context
from app.orchestrator import tools as tools_mod


def _id_token(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"x.{body}.y"


@pytest.fixture
def clients(monkeypatch):
    monkeypatch.setattr(oauth, "client", lambda p: {"client_id": f"{p.name}-id", "client_secret": "s"})
    oauth._flows.clear()
    yield
    oauth._flows.clear()


# ---- Google ----
def test_google_start_uses_pkce_offline_access_and_the_loopback(clients):
    start = oauth.google_start("u1", "me@gmail.com")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(start["url"]).query)
    assert start["paste"] is True
    assert query["redirect_uri"] == [oauth.LOOPBACK]
    assert query["code_challenge_method"] == ["S256"] and query["access_type"] == ["offline"]
    assert "https://mail.google.com/" in query["scope"][0]
    assert query["login_hint"] == ["me@gmail.com"]


def test_google_finish_exchanges_the_pasted_address(clients, monkeypatch):
    start = oauth.google_start("u1")
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(start["url"]).query)["state"][0]
    sent = {}

    def post(url, data):
        sent.update(data)
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600,
                "id_token": _id_token({"email": "me@gmail.com"})}

    monkeypatch.setattr(oauth, "_post", post)
    code, got_state = oauth.code_from(f"http://127.0.0.1:8765/?state={state}&code=4/abc&scope=x")
    secret = oauth.google_finish("u1", code, got_state)
    assert sent["code"] == "4/abc" and sent["code_verifier"] and sent["grant_type"] == "authorization_code"
    assert secret["username"] == "me@gmail.com" and secret["imap_host"] == "imap.gmail.com"
    assert secret["auth"] == "oauth" and secret["refresh_token"] == "rt" and secret["password"] == ""
    with pytest.raises(oauth.OAuthError):  # used once
        oauth.google_finish("u1", code, got_state)


def test_google_finish_refuses_another_sign_ins_state(clients, monkeypatch):
    oauth.google_start("u1")
    monkeypatch.setattr(oauth, "_post", lambda *a: pytest.fail("must not exchange"))
    with pytest.raises(oauth.OAuthError, match="different sign-in"):
        oauth.google_finish("u1", "code", "someone-elses-state")


def test_pasting_something_without_a_code_is_explained():
    with pytest.raises(oauth.OAuthError, match="code="):
        oauth.code_from("https://example.com/whatever")
    with pytest.raises(oauth.OAuthError, match="access_denied"):
        oauth.code_from("http://127.0.0.1:8765/?error=access_denied&code=")


def test_not_configured_tells_the_admin_what_to_add(monkeypatch):
    monkeypatch.setattr(oauth, "client", lambda p: None)
    with pytest.raises(oauth.OAuthError, match="oauth/google"):
        oauth.google_start("u1")


# ---- Microsoft ----
def test_microsoft_device_code_waits_then_connects(clients, monkeypatch):
    calls = []

    def post(url, data):
        calls.append((url, data))
        if url.endswith("/devicecode"):
            return {"device_code": "dc", "user_code": "ABCD", "verification_uri": "https://microsoft.com/devicelogin",
                    "interval": 1, "expires_in": 900}
        if len(calls) == 2:
            return {"error": "authorization_pending"}
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600,
                "id_token": _id_token({"preferred_username": "me@outlook.com"})}

    monkeypatch.setattr(oauth, "_post", post)
    start = oauth.microsoft_start("u2")
    assert start["user_code"] == "ABCD" and "/common/" in calls[0][0]
    assert "IMAP.AccessAsUser.All" in calls[0][1]["scope"] and "offline_access" in calls[0][1]["scope"]
    assert oauth.microsoft_poll("u2") is None
    oauth._flows["u2"]["next"] = 0
    secret = oauth.microsoft_poll("u2")
    assert secret["username"] == "me@outlook.com" and secret["imap_host"] == "outlook.office365.com"
    assert secret["smtp_host"] == "smtp.office365.com" and secret["smtp_port"] == 587


def test_expired_access_token_is_refreshed_and_rotated(clients, monkeypatch):
    sent = {}

    def post(url, data):
        sent.update(data)
        return {"access_token": "new", "refresh_token": "rt2", "expires_in": 3600}

    monkeypatch.setattr(oauth, "_post", post)
    data = {"provider": "microsoft", "access_token": "old", "refresh_token": "rt1", "expires_at": int(time.time()) - 5}
    assert oauth.access_token("", data) == "new"
    assert sent["grant_type"] == "refresh_token" and sent["refresh_token"] == "rt1"
    fresh = {**data, "access_token": "still-good", "expires_at": int(time.time()) + 3000}
    assert oauth.access_token("", fresh) == "still-good"


# ---- connector: XOAUTH2 instead of a password ----
def test_oauth_mailbox_logs_in_with_xoauth2(monkeypatch):
    raw = json.dumps({"auth": "oauth", "provider": "google", "imap_host": "imap.gmail.com", "imap_port": 993,
                      "smtp_host": "smtp.gmail.com", "smtp_port": 465, "username": "me@gmail.com",
                      "from": "me@gmail.com", "password": "", "refresh_token": "rt", "access_token": "at",
                      "expires_at": int(time.time()) + 3000})
    cfg = connector.MailConfig.from_secret(raw, name="mail/personal/u1")
    assert cfg.oauth_provider == "google" and cfg.method == "google sign-in" and cfg.password == ""
    seen = {}

    class FakeIMAP:
        def __init__(self, *a, **k):
            pass

        def authenticate(self, mech, fn):
            seen["mech"], seen["auth"] = mech, fn(b"")

        def login(self, *a):
            pytest.fail("no password login for an OAuth mailbox")

    monkeypatch.setattr(connector.imaplib, "IMAP4_SSL", FakeIMAP)
    connector._imap(cfg)
    assert seen["mech"] == "XOAUTH2" and seen["auth"] == b"user=me@gmail.com\x01auth=Bearer at\x01\x01"


def test_an_oauth_secret_without_a_refresh_token_is_rejected():
    raw = json.dumps({"auth": "oauth", "imap_host": "h", "username": "u", "password": ""})
    with pytest.raises(ValueError):
        connector.MailConfig.from_secret(raw)


def test_password_mailboxes_still_work_the_old_way():
    cfg = connector.MailConfig.from_secret(json.dumps({"imap_host": "imap.gmail.com", "username": "a@gmail.com",
                                                       "password": "abcd"}))
    assert cfg.method == "password" and cfg.password == "abcd" and cfg.oauth is None


# ---- dashboards ----
SPEC = {
    "title": "Sales", "kpis": [{"label": "Revenue", "value": "₹4 L", "change": "+3%", "trend": "up"}],
    "charts": [{"type": "pie", "title": "Share", "labels": ["A", "B"], "series": [{"name": "Share", "values": ["60", 40]}]}],
    "tables": [{"columns": ["Name", "Units"], "rows": [{"Name": "<script>alert(1)</script>", "Units": 3}]}],
    "notes": "ok",
}


def test_dashboard_is_cleaned_into_what_the_ui_draws():
    dash = dashboards.normalize(json.dumps(SPEC))
    chart = dash["charts"][0]
    assert chart["type"] == "hbar" and chart["pie"] and chart["series"][0]["values"] == [60.0, 40.0]
    assert dash["tables"][0]["rows"] == [["<script>alert(1)</script>", 3]]  # text, drawn as text by the UI
    assert dash["kpis"][0]["trend"] == "up" and len(dash["id"]) == 12


@pytest.mark.parametrize("bad, words", [
    ({"charts": [{"labels": ["a", "b"], "series": [{"name": "x", "values": [1]}]}]}, "one value per label"),
    ({"charts": [{"type": "radar", "labels": ["a"], "series": [{"values": [1]}]}]}, "bar, hbar or line"),
    ({"charts": [{"labels": ["a"], "series": [{"name": str(i), "values": [1]} for i in range(9)]}]}, "Other"),
    ({}, "empty"),
    ("{not json", "not valid JSON"),
])
def test_bad_dashboards_say_how_to_fix_them(bad, words):
    with pytest.raises(ValueError, match=words):
        dashboards.normalize(bad if isinstance(bad, str) else json.dumps(bad))


def test_make_dashboard_shows_it_under_the_reply(monkeypatch):
    monkeypatch.setattr(tools_mod, "_enforce", lambda *a: None)
    made: list = []
    token = context.dashboards.set(made)
    try:
        out = tools_mod.make_dashboard({}, "Sales", json.dumps(SPEC))
        assert "shown to the person" in out and len(made) == 1
        final = context.with_dashboards({"role": "assistant", "content": "Takeaways"})
        assert final["dashboards"][0]["title"] == "Sales"
        assert "ERROR" in tools_mod.make_dashboard({}, "Sales", '{"charts": [{"type": "radar"}]}')
    finally:
        context.dashboards.reset(token)
    assert "only be shown in a chat" in tools_mod.make_dashboard({}, "Sales", json.dumps(SPEC))
    assert context.with_dashboards({"role": "assistant", "content": "x"}) == {"role": "assistant", "content": "x"}


def test_object_arguments_reach_string_parameters_as_json():
    args = tools_mod.coerce_args(tools_mod.make_dashboard, {"title": "t", "spec": {"kpis": []}})
    assert args["spec"] == '{"kpis": []}'


# ---- sandbox ----
needs_sandbox = pytest.mark.skipif(not sandbox.available(), reason="sandbox interpreter not installed")


@needs_sandbox
def test_run_python_analyses_input_files_and_makes_a_dashboard(monkeypatch):
    monkeypatch.setattr(tools_mod, "_enforce", lambda *a: None)
    monkeypatch.setattr(tools_mod, "_input_bytes", lambda state, path: ("sales.csv", b"region,v\nN,1\nS,2\nN,3\n"))
    made: list = []
    token = context.dashboards.set(made)
    try:
        code = (
            "import pandas as pd, json\n"
            "df = pd.read_csv('input/sales.csv')\n"
            "totals = df.groupby('region')['v'].sum()\n"
            "print(totals.to_dict())\n"
            "json.dump({'title': 'By region', 'charts': [{'type': 'bar', 'labels': list(totals.index), "
            "'series': [{'name': 'v', 'values': [int(x) for x in totals]}]}]}, open('dashboard.json', 'w'))\n"
        )
        out = tools_mod.run_python({"scratchpad": {}}, code, "sales.csv")
    finally:
        context.dashboards.reset(token)
    assert "{'N': 4, 'S': 2}" in out and "input/sales.csv" in out
    assert made and made[0]["charts"][0]["labels"] == ["N", "S"]


@needs_sandbox
@pytest.mark.parametrize("code", [
    "import socket; socket.socket().connect(('127.0.0.1', 6379))",
    "import subprocess; subprocess.run(['id'])",
    "import os; os.system('id')",
    "open('/tmp/escape', 'w').write('x')",
    "import ctypes; ctypes.CDLL(None)",
    "print(open('/root/myai/.env').read())",
])
def test_the_sandbox_refuses_network_programs_and_outside_files(code):
    result = sandbox.run(code)
    assert not result.ok
    assert "PermissionError" in result.stderr or "Permission denied" in result.stderr


@needs_sandbox
def test_the_sandbox_stops_runaway_code(monkeypatch):
    monkeypatch.setattr(sandbox, "WALL_SECONDS", 3)
    result = sandbox.run("while True:\n    pass\n")
    assert not result.ok and (result.timed_out or not result.stderr)
