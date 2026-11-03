"""Saved personal details (only the owner, filled into the right sites without the model seeing
them), secrets typed through the secure box, "personal or work?" on every order, and mail set up
from just an address."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import wallet
from app.auth.security import hash_password, issue_token
from app.db.session import Base, DepartmentTask, User, WalletItem, get_session
from app.departments.registry import seed_departments
from app.llm import client as llm
from app.mail import providers
from app.main import app
from app.orchestrator import execution, tools
from app.orchestrator.context import user_key
from app.orchestrator.graph import build_graph

KEY = os.urandom(32)
CARD = {"label": "HDFC", "name": "Ravi Kumar", "number": "4111 1111 1111 1111", "expiry": "08/28"}


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        seed_departments(s)
        s.add(User(id="alice", username="alice", password_hash=hash_password("alice-password-1"), roles=["hr"], prefs={}))
        s.commit()
    monkeypatch.setattr(tools, "SessionLocal", Session)
    return Session


def _private():
    return {"subject_id": "alice", "subject_roles": ["hr"], "scratchpad": {"mode": "single"}, "messages": []}


# ---- the wallet ----
def test_details_are_validated_encrypted_and_masked(db):
    with db() as s:
        card = wallet.add(s, KEY, "alice", "card", CARD)
        login = wallet.add(s, KEY, "alice", "login", {"site": "https://www.amazon.in/", "username": "ravi@gmail.com", "password": "S3cret!"})
        stored = " ".join(r.encrypted for r in s.query(WalletItem).all())
        assert "4111" not in stored and "S3cret" not in stored and "ravi@" not in stored
        listing = {i["id"]: i["summary"] for i in wallet.listing(s, KEY, "alice")}
        assert listing[card] == "HDFC •••• 1111, expires 08/28 (Ravi Kumar)"
        assert listing[login] == "Login for amazon.in (ra•••@gmail.com)"
        assert wallet.listing(s, os.urandom(32), "alice") == [] and wallet.listing(s, KEY, "bob") == []
        with pytest.raises(wallet.WalletError):
            wallet.get(s, KEY, "bob", card)  # someone else's id looks like a missing one


@pytest.mark.parametrize(("kind", "data", "why"), [
    ("card", {**CARD, "number": "4111 1111 1111 1112"}, "isn't valid"),
    ("card", {**CARD, "expiry": "13/28"}, "expiry"),
    ("card", {**CARD, "cvv": "123"}, "never saved"),
    ("login", {"site": "amazon.in", "username": "x"}, "needs"),
    ("upi", {"upi_id": "not a upi"}, "UPI"),
])
def test_bad_details_are_refused_and_cvvs_never_kept(db, kind, data, why):
    with db() as s, pytest.raises(wallet.WalletError, match=why):
        wallet.add(s, KEY, "alice", kind, data)


@pytest.mark.parametrize(("kind", "url", "ok"), [
    ("login", "https://www.amazon.in/ap/signin", True),
    ("login", "https://amazon.in.evil.example/signin", False),
    ("login", "https://www.flipkart.com/login", False),
    ("card", "https://api.razorpay.com/checkout", True),
    ("card", "https://www.amazon.in/gp/buy", True),
    ("card", "https://free-gift.example/claim", False),
])
def test_details_are_typed_only_on_the_right_sites(kind, url, ok):
    data = {"site": "amazon.in"} if kind == "login" else {"number": "4111111111111111"}
    assert (wallet.allowed_on(kind, data, url) is None) is ok


def test_registrable_domains():
    assert wallet.site_of("https://pay.irctc.co.in/x") == "irctc.co.in"
    assert wallet.site_of("https://www.amazon.in/") == "amazon.in"


# ---- the tools ----
class FakeBrowser:
    def __init__(self, url):
        self.url, self.typed = url, []

    def has(self, key):
        return True

    def call(self, fn, *args):
        return fn(*args)

    def fill_value(self, key, ref, value, page_ok):
        why = page_ok(self.url)
        if why:
            from app.browsing.browser import BrowserError

            raise BrowserError(f"not filled: {why}")
        self.typed.append((ref, value))
        return f"Filled [{ref}]."

    def url_of(self, key):
        return self.url

    def state_of(self, key):
        return None


def _with_browser(monkeypatch, url):
    from app.browsing import browser as browser_mod

    fake = FakeBrowser(url)
    monkeypatch.setattr(browser_mod, "browser", lambda: fake)
    monkeypatch.setattr(tools, "_enforce", lambda *a: None)
    return fake


def test_the_agent_fills_saved_details_without_seeing_them(db, monkeypatch):
    fake = _with_browser(monkeypatch, "https://www.amazon.in/ap/signin")
    with db() as s:
        login = wallet.add(s, KEY, "alice", "login", {"site": "amazon.in", "username": "ravi@gmail.com", "password": "S3cret!"})
        card = wallet.add(s, KEY, "alice", "card", CARD)
    token = user_key.set(KEY)
    try:
        listing = tools.list_saved_details(_private())
        assert login in listing and "S3cret" not in listing and "4111 1111" not in listing
        out = tools.fill_saved(_private(), login, "password", 7)
        assert "S3cret" not in out and fake.typed == [(7, "S3cret!")]
        fake.url = "https://amazon-deals.example/login"  # a look-alike page: nothing typed
        assert "not filled" in tools.fill_saved(_private(), login, "password", 7)
        fake.url = "https://www.amazon.in/gp/buy/payselect"
        tools.fill_saved(_private(), card, "expiry_month", 9)
        assert fake.typed[-1] == (9, "08")
    finally:
        user_key.reset(token)


def test_work_requests_never_reach_personal_details(db, monkeypatch):
    _with_browser(monkeypatch, "https://www.amazon.in/")
    work = {"subject_id": "alice", "subject_roles": ["account"], "scratchpad": {"department": "account"}}
    assert "only used in the person's private chat" in tools.list_saved_details(work)
    assert "only used in the person's private chat" in tools.fill_saved(work, "x", "password", 1)


def test_the_admin_cannot_open_anyone_elses_wallet(db):
    from app.acl.engine import ACLEngine
    from app.acl.models import Action, Resource, Subject

    with db() as s:
        acl = ACLEngine(s)
        assert not acl.check(Subject("root", ("admin",)), Action.READ, Resource("wallet", "alice/x")).allowed
        assert acl.check(Subject("alice", ("hr",)), Action.READ, Resource("wallet", "alice/x")).allowed


def test_a_secret_is_asked_through_the_secure_box(db, monkeypatch):
    from app.orchestrator.context import browser_request

    _with_browser(monkeypatch, "https://secure.payu.in/otp")
    holder: dict = {}
    token, ktoken = browser_request.set(holder), user_key.set(KEY)
    try:
        assert "secure box" in tools.ask_person_for_secret(_private(), 4, "the OTP sent to your phone")
    finally:
        browser_request.reset(token)
        user_key.reset(ktoken)
    assert holder == {"scope": "private", "secret": {"ref": 4, "what": "the OTP sent to your phone", "site": "payu.in"}}


def test_the_secret_endpoint_types_only_on_the_same_site(monkeypatch):
    from app.acl.models import Subject
    from app.api import deps, routes_browser

    fake = FakeBrowser("https://secure.payu.in/otp")
    monkeypatch.setattr(routes_browser, "browser", lambda: fake)
    monkeypatch.setattr(routes_browser.logins, "persist", lambda *a, **k: None)
    app.dependency_overrides[deps.current_subject] = lambda: Subject("alice", ("hr",))
    try:
        client = TestClient(app)
        h = {"authorization": "Bearer " + issue_token("alice", "alice", ["hr"], chat_key=KEY)}
        assert client.post("/v1/browser/private/secret", headers=h, json={"ref": 4, "value": "123456", "site": "payu.in"}).status_code == 200
        assert fake.typed == [(4, "123456")]
        fake.url = "https://phish.example/otp"
        r = client.post("/v1/browser/private/secret", headers=h, json={"ref": 4, "value": "123456", "site": "payu.in"})
        assert r.status_code == 409 and len(fake.typed) == 1
    finally:
        app.dependency_overrides.clear()


# ---- every order: personal or work ----
@pytest.fixture
def chat(db, monkeypatch):
    def _get_session():
        with db() as s:
            yield s

    saver = MemorySaver()
    plain = saver.serde

    @contextmanager
    def _cm(mode, serde=None):
        saver.serde = serde or plain
        yield build_graph().compile(checkpointer=saver)

    monkeypatch.setattr(execution, "_graph_cm", _cm)
    app.dependency_overrides[get_session] = _get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_an_order_card_asks_personal_or_work_and_work_goes_to_accounts(chat, db, monkeypatch):
    clicked = []
    monkeypatch.setitem(tools.TOOLS, "browser_commit", lambda state, ref, label, summary: clicked.append(label) or "placed")
    call = json.dumps({"tool": "browser_commit", "args": {"ref": 12, "label": "Place your order",
                                                          "summary": "1 x boAt earbuds, ₹1,099, to Home"}})
    replies = [call, "It's with Accounts now."]
    monkeypatch.setattr(llm, "chat", lambda messages, **kw: replies.pop(0) if replies else "ok")
    h = {"authorization": "Bearer " + issue_token("alice", "alice", ["hr"], chat_key=KEY)}
    body = chat.post("/v1/chat", headers=h, json={"message": "order the boAt earbuds", "thread_id": "o1"}).json()
    assert body["outcome"] == "confirm" and body["confirm"]["purchase"] is True
    done = chat.post("/v1/chat/threads/o1/confirm", headers=h, json={"approve": True, "as_work": True}).json()
    assert done["reply"] == "It's with Accounts now." and clicked == []  # nothing bought with personal details
    with db() as s:
        task = s.query(DepartmentTask).one()
    assert task.department == "account" and task.kind == "order" and "boAt" in task.message


# ---- mail from just an address ----
def test_gmail_is_tried_first_and_outlook_explains_itself():
    assert [p.name for p in providers.candidates("ravi@gmail.com")] == ["Gmail"]
    assert [p.name for p in providers.candidates("ravi@mycompany.in")] == ["Gmail", "Outlook"]
    assert providers.candidates("ravi@hotmail.com")[0].password_ok is False
    assert "apppasswords" in providers.explain(providers.GMAIL, "b'[AUTHENTICATIONFAILED] Invalid credentials'")
