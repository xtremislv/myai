"""The approval and inbox flows over HTTP, with real JWTs and ACL checks. The LLM is
faked; SQLite and in-memory checkpoints replace Postgres."""
from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.security import hash_password, issue_token, verify_password
from app.db.session import Base, get_session
from app.departments.registry import seed_departments
from app.llm import client as llm
from app.main import app
from app.orchestrator import execution, tools
from app.orchestrator.graph import build_graph
from app.request_manager import quota, router

BILL_CALL = json.dumps({"tool": "manage_bill", "args": {"bill_id": "B1"}})


def _auth(user_id, *roles):
    return {"authorization": f"Bearer {issue_token(user_id, user_id, list(roles))}"}


ALICE = _auth("alice", "account")
ADMIN = _auth("root", "admin")


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        seed_departments(s)

    def _get_session():
        with Session() as s:
            yield s

    saver = MemorySaver()

    @contextmanager
    def _cm(mode):
        yield build_graph().compile(checkpointer=saver)

    monkeypatch.setattr(execution, "_graph_cm", _cm)
    limiter = quota.InMemoryQuota()
    monkeypatch.setattr(router, "default_quota", lambda: limiter)
    app.dependency_overrides[get_session] = _get_session
    yield TestClient(app)  # not entered as a context manager: skips startup
    app.dependency_overrides.clear()


@pytest.fixture
def executed(monkeypatch):
    calls = []
    monkeypatch.setitem(tools.TOOLS, "manage_bill", lambda state, bill_id: calls.append(bill_id) or "ok")
    return calls


def _script(monkeypatch, *replies):
    queue = list(replies)
    monkeypatch.setattr(llm, "chat", lambda messages, **k: queue.pop(0))


def _submit(client, message="record the ACME ledger entry", headers=ALICE, thread="t1"):
    return client.post("/v1/request", headers=headers, json={"message": message, "thread_id": thread})


def test_approve_over_http_runs_the_tool_once(client, executed, monkeypatch):
    _script(monkeypatch, BILL_CALL, "Recorded.")

    body = _submit(client).json()
    assert body["outcome"] == "pending_approval"
    assert executed == []

    listed = client.get("/v1/approvals", headers=ADMIN).json()
    assert [(a["id"], a["tool"], a["tool_args"]) for a in listed] == [
        (body["approval_id"], "manage_bill", {"bill_id": "B1"})
    ]

    done = client.post(f"/v1/approvals/{body['approval_id']}/approve", headers=ADMIN)
    assert done.status_code == 200
    assert (done.json()["outcome"], done.json()["reply"]) == ("ok", "Recorded.")
    assert executed == ["B1"]

    again = client.post(f"/v1/approvals/{body['approval_id']}/approve", headers=ADMIN)
    assert again.status_code == 409
    assert executed == ["B1"]


def test_deny_over_http_does_not_run_the_tool(client, executed, monkeypatch):
    _script(monkeypatch, BILL_CALL, "Understood, not recorded.")
    approval_id = _submit(client).json()["approval_id"]

    done = client.post(f"/v1/approvals/{approval_id}/deny", headers=ADMIN, json={"reason": "wrong vendor"})

    assert done.json()["outcome"] == "denied"
    assert executed == []
    assert client.get("/v1/approvals", headers=ADMIN).json() == []


def test_non_admin_cannot_decide(client, executed, monkeypatch):
    _script(monkeypatch, BILL_CALL)
    approval_id = _submit(client).json()["approval_id"]
    assert client.post(f"/v1/approvals/{approval_id}/approve", headers=ALICE).status_code == 403
    assert executed == []


def test_unknown_approval_is_404(client):
    assert client.post("/v1/approvals/nope/approve", headers=ADMIN).status_code == 404


def test_inbox_payment_over_http_needs_approval_and_cannot_be_claimed_twice(client, executed, monkeypatch):
    task_id = _submit(client, "pay the ACME invoice", _auth("u1", "logistics")).json()["task_id"]
    _script(monkeypatch, BILL_CALL, "Recorded.")

    queued = client.get("/v1/inbox", params={"department": "account"}, headers=ALICE).json()
    assert [t["id"] for t in queued] == [task_id]

    res = client.post(f"/v1/inbox/{task_id}/process", headers=ALICE).json()
    assert res["outcome"] == "pending_approval"
    assert executed == []

    assert client.post(f"/v1/inbox/{task_id}/process", headers=ALICE).status_code == 409
    assert client.get("/v1/inbox", params={"department": "account"}, headers=ALICE).json() == []

    client.post(f"/v1/approvals/{res['approval_id']}/approve", headers=ADMIN)
    assert executed == ["B1"]


def test_inbox_task_returns_to_the_queue_when_the_run_errors(client, monkeypatch):
    task_id = _submit(client, "place an order for laptops", _auth("u1", "logistics")).json()["task_id"]

    def _down(messages, **k):
        raise ConnectionError("vLLM is down")

    monkeypatch.setattr(llm, "chat", _down)
    res = client.post(f"/v1/inbox/{task_id}/process", headers=ALICE).json()
    assert res["outcome"] == "error"
    queued = client.get("/v1/inbox", params={"department": "account"}, headers=ALICE).json()
    assert [t["id"] for t in queued] == [task_id]


@pytest.mark.parametrize("thread", ["", "x" * 129])
def test_invalid_thread_id_is_rejected(client, thread):
    assert _submit(client, thread=thread).status_code == 422


def test_login_with_a_real_password_hash(client):
    created = client.post(
        "/v1/auth/users", headers=ADMIN, json={"username": "bob", "password": "correct horse", "roles": ["hr"]}
    )
    assert created.status_code == 201

    good = client.post("/v1/auth/login", json={"username": "bob", "password": "correct horse"})
    assert good.status_code == 200 and good.json()["access_token"]
    bad = client.post("/v1/auth/login", json={"username": "bob", "password": "wrong"})
    assert bad.status_code == 401


def test_passwords_longer_than_bcrypt_allows_still_work():
    long_password = "x" * 200
    assert verify_password(long_password, hash_password(long_password))
    assert not verify_password("y" * 200, hash_password(long_password))


def test_boot_aborts_on_insecure_secrets(monkeypatch):
    from app import main

    monkeypatch.setattr(main._settings, "jwt_secret", "change-me-in-prod")
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        with TestClient(main.app):
            pass
