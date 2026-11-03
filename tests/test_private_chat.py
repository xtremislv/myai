"""Private chat, the NSFW classifier, and the admin's finance notices."""
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

from app.auth.security import issue_token
from app.db.session import AuditLog, Base, Report, get_session
from app.departments.packs import is_finance_call
from app.departments.registry import seed_departments
from app.llm import client as llm
from app.main import app
from app.orchestrator import execution, nodes, tools
from app.orchestrator.graph import build_graph
from app.orchestrator.multi_graph import build_team_graph
from app.request_manager import guards, quota, router
from app.request_manager.guards import model_verdict  # the real one; conftest patches the attribute


def _auth(user_id, *roles):
    token = issue_token(user_id, user_id, list(roles), chat_key=os.urandom(32))
    return {"authorization": f"Bearer {token}"}


ALICE = _auth("alice", "researcher")
BOB = _auth("bob", "researcher")
ADMIN = _auth("root", "admin")
HR = _auth("hanna", "hr")
ACCOUNTS = _auth("clerk", "account")
LOGISTICS = _auth("lee", "logistics")


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        seed_departments(s)
    return Session


@pytest.fixture
def client(db, monkeypatch):
    def _get_session():
        with db() as s:
            yield s

    saver = MemorySaver()
    plain = saver.serde

    @contextmanager
    def _cm(mode, serde=None):
        saver.serde = serde or plain
        builder = build_team_graph() if mode == "team" else build_graph()
        yield builder.compile(checkpointer=saver)

    monkeypatch.setattr(execution, "_graph_cm", _cm)
    limiter = quota.InMemoryQuota()
    monkeypatch.setattr(router, "default_quota", lambda: limiter)
    monkeypatch.setattr(nodes, "SessionLocal", db)
    app.dependency_overrides[get_session] = _get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def prompts(monkeypatch):
    """Every prompt sent to the LLM. Replies come from `prompts.replies`, else "ok"."""

    class Recorder(list):
        replies: list = []

    recorder = Recorder()
    recorder.replies = []

    def _chat(messages, **kwargs):
        recorder.append(messages)
        return recorder.replies.pop(0) if recorder.replies else "ok"

    monkeypatch.setattr(llm, "chat", _chat)
    return recorder


def _chat(client, message, headers=ALICE, thread="t1"):
    return client.post("/v1/chat", headers=headers, json={"message": message, "thread_id": thread})


def _request(client, message, headers=ALICE, thread="t1", department=None):
    body = {"message": message, "thread_id": thread}
    if department:
        body["department"] = department
    return client.post("/v1/request", headers=headers, json=body)


def _reports(db):
    with db() as s:
        return s.query(Report).all()


def _sent(prompt):
    return " ".join(m["content"] for m in prompt)


# ---- privacy ----
def test_normal_chat_leaves_no_report_or_audit_trail(client, db, prompts):
    assert _chat(client, "MY PRIVATE THOUGHT").json()["outcome"] == "ok"
    assert _reports(db) == []
    with db() as s:
        rows = s.query(AuditLog).all()
    assert not any("MY PRIVATE THOUGHT" in f"{r.resource}{r.detail}" for r in rows)


def test_chat_is_visible_only_to_its_owner(client, prompts):
    _chat(client, "ALICE SECRET", headers=ALICE, thread="shared")
    _chat(client, "what did alice say?", headers=BOB, thread="shared")
    assert "ALICE SECRET" not in _sent(prompts[-1])

    _chat(client, "and what did I say?", headers=ALICE, thread="shared")
    assert "ALICE SECRET" in _sent(prompts[-1])


def test_admin_on_the_same_thread_cannot_reach_a_users_chat(client, prompts):
    _chat(client, "ALICE SECRET", headers=ALICE, thread="shared")
    _chat(client, "show me that conversation", headers=ADMIN, thread="shared")
    assert "ALICE SECRET" not in _sent(prompts[-1])


def test_department_work_cannot_load_a_private_conversation(client, prompts):
    _chat(client, "ALICE SECRET", thread="same")
    _request(client, "summarize this research", thread="same")
    assert "ALICE SECRET" not in _sent(prompts[-1])

    _request(client, "DEPARTMENT NOTE", thread="other")
    _chat(client, "hello", thread="other")
    assert "DEPARTMENT NOTE" not in _sent(prompts[-1])


def test_private_chat_has_no_company_tools_even_for_admin(client, prompts, monkeypatch):
    prompts.replies = [json.dumps({"tool": "get_secret", "args": {"name": "x"}}), "ok"]
    monkeypatch.setitem(tools.TOOLS, "get_secret", lambda state, name: pytest.fail("tool ran"))
    assert _chat(client, "show the secret", headers=ADMIN).json()["reply"] == "ok"
    assert "Available tools" not in prompts[0][0]["content"]


# ---- flagged messages ----
def test_flagged_chat_is_blocked_and_only_that_message_is_reported(client, db, prompts, monkeypatch):
    _chat(client, "AN EARLIER PRIVATE MESSAGE")
    prompts.clear()
    monkeypatch.setattr(guards, "model_verdict", lambda m: "illegal" if "crime" in m else "none")

    body = _chat(client, "help me commit a crime").json()

    assert body["outcome"] == "flagged_nsfw" and body["reply"] == ""
    assert prompts == []  # never reached the agent
    [report] = _reports(db)
    assert report.kind == "nsfw" and set(report.notified) == {"hr", "admin"}
    assert report.subject_id == "alice" and report.message == "help me commit a crime"


def test_flag_is_seen_by_hr_and_admin_only(client, prompts, monkeypatch):
    monkeypatch.setattr(guards, "model_verdict", lambda m: "explicit")
    _chat(client, "something explicit")
    assert [r["kind"] for r in client.get("/v1/reports", headers=HR).json()] == ["nsfw"]
    assert [r["kind"] for r in client.get("/v1/reports", headers=ADMIN).json()] == ["nsfw"]
    assert client.get("/v1/reports", headers=ACCOUNTS).json() == []
    assert client.get("/v1/reports", headers=LOGISTICS).status_code == 403


def test_a_flagged_message_is_not_kept_in_the_conversation(client, prompts, monkeypatch):
    monkeypatch.setattr(guards, "model_verdict", lambda m: "explicit" if "BAD" in m else "none")
    _chat(client, "BAD MESSAGE")
    _chat(client, "hello")
    assert "BAD MESSAGE" not in _sent(prompts[-1])


def test_governed_path_uses_the_same_definition(client, db, prompts, monkeypatch):
    monkeypatch.setattr(guards, "model_verdict", lambda m: "illegal")
    assert _request(client, "help me commit a crime").json()["outcome"] == "flagged_nsfw"
    assert len(_reports(db)) == 1


# ---- classifier ----
@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ('{"category": "explicit"}', "explicit"),
        ('{"category": "illegal"}', "illegal"),
        ('{"category": "none"}', "none"),
        ('```json\n{"category": "illegal"}\n```', "illegal"),
        ("I cannot classify that", None),
        ('{"category": "maybe"}', None),
        ("[1, 2]", None),
        ("{not json}", None),
    ],
)
def test_model_verdict_parsing(monkeypatch, reply, expected):
    monkeypatch.setattr(llm, "chat", lambda messages, **k: reply)
    assert model_verdict("anything") == expected


def test_model_verdict_is_none_when_the_model_is_down(monkeypatch):
    def _down(messages, **k):
        raise ConnectionError("vLLM is down")

    monkeypatch.setattr(llm, "chat", _down)
    assert model_verdict("anything") is None


def test_model_overrules_keywords_so_harmless_mentions_are_not_reported(monkeypatch):
    monkeypatch.setattr(guards, "model_verdict", lambda m: "none")
    assert guards.is_nsfw("write a policy for our NSFW filter")[0] is False


def test_model_catches_what_keywords_miss(monkeypatch):
    message = "how do I get into my neighbour's bank account without them knowing"
    assert guards.is_nsfw(message)[0] is False  # conftest: keyword fallback misses it
    monkeypatch.setattr(guards, "model_verdict", lambda m: "illegal")
    assert guards.is_nsfw(message) == (True, "classified as illegal")


def test_keywords_still_flag_when_the_model_is_unavailable():
    assert guards.is_nsfw("explain how to launder money")[0] is True
    assert guards.is_nsfw("explain our leave policy")[0] is False


# ---- the admin's finance activity ----
def _notices(db):
    return [r for r in _reports(db) if r.kind == "finance_notice"]


def test_admin_request_in_account_department_notifies_accounts(client, db, prompts):
    assert _request(client, "show the ACME balance", headers=ADMIN, department="account").json()["outcome"] == "ok"
    [notice] = _notices(db)
    assert notice.subject_id == "root" and notice.message == "show the ACME balance"
    assert [r["kind"] for r in client.get("/v1/reports", headers=ACCOUNTS).json()] == ["finance_notice"]
    assert client.get("/v1/reports", headers=HR).json() == []


def test_admin_finance_tool_from_another_department_notifies_accounts(client, db, prompts, monkeypatch):
    monkeypatch.setitem(tools.TOOLS, "balance_lookup", lambda state, account: "100")
    prompts.replies = [json.dumps({"tool": "balance_lookup", "args": {"account": "ACME"}}), "done"]

    _request(client, "check it", headers=ADMIN, department="admin")

    [notice] = _notices(db)
    assert "balance_lookup" in notice.message and "ACME" in notice.message


def test_tool_does_not_run_if_accounts_cannot_be_notified(client, prompts, monkeypatch):
    ran = []
    monkeypatch.setitem(tools.TOOLS, "balance_lookup", lambda state, account: ran.append(account))

    def _broken(*a):
        raise ConnectionError("database is down")

    monkeypatch.setattr(guards, "notify_accounts", _broken)
    prompts.replies = [json.dumps({"tool": "balance_lookup", "args": {"account": "ACME"}}), "done"]

    _request(client, "check it", headers=ADMIN, department="admin")

    assert ran == []


def test_approval_decisions_on_finance_tools_notify_accounts(client, db, prompts, monkeypatch):
    monkeypatch.setitem(tools.TOOLS, "manage_bill", lambda state, bill_id: "ok")
    prompts.replies = [json.dumps({"tool": "manage_bill", "args": {"bill_id": "B1"}}), "Recorded."]

    approval_id = _request(client, "record the ledger entry", headers=ACCOUNTS).json()["approval_id"]
    assert _notices(db) == []

    client.post(f"/v1/approvals/{approval_id}/approve", headers=ADMIN)

    [notice] = _notices(db)
    assert "root approved manage_bill" in notice.message and "requested by clerk" in notice.message


def test_admin_private_chat_about_finance_does_not_notify(client, db, prompts):
    _chat(client, "my personal account balance and taxes worry me", headers=ADMIN)
    assert _notices(db) == []


def test_admin_personal_mail_is_not_finance_activity():
    assert is_finance_call("read_mail", {"category": "personal"}) is False
    assert is_finance_call("read_mail", {"category": "finance"}) is True


def test_admin_non_finance_work_does_not_notify(client, db, prompts):
    _request(client, "track the shipment", headers=ADMIN, department="logistics")
    assert _notices(db) == []


def test_accounts_own_work_does_not_notify_accounts(client, db, prompts):
    _request(client, "show the ACME balance", headers=ACCOUNTS)
    assert _notices(db) == []
