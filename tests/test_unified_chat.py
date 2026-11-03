"""One chat box: triage sorts each message into private or work. Multiple departments."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.security import issue_token
from app.db.session import AuditLog, Base, Report, get_session
from app.departments.registry import seed_departments
from app.llm import client as llm
from app.main import app
from app.orchestrator import execution, nodes, tools
from app.request_manager import guards, quota, router
from app.request_manager.guards import (
    triage_verdict,  # the real one; conftest patches the attribute
)

KEY = os.urandom(32)
ADMIN = {"authorization": f"Bearer {issue_token('root', 'root', ['admin'], chat_key=os.urandom(32))}"}


def _auth(user, *roles):
    return {"authorization": f"Bearer {issue_token(user, user, list(roles), chat_key=KEY)}"}


LEE = _auth("lee", "logistics")
MULTI = _auth("sam", "logistics", "researcher")
NOBODY = _auth("nia")


@pytest.fixture
def api(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        seed_departments(s)

    def _get_session():
        with Session() as s:
            yield s

    saver = MemorySaver()
    plain = saver.serde

    @contextmanager
    def _cm(mode, serde=None):
        saver.serde = serde or plain
        builder = execution.builder(mode)
        yield builder.compile(checkpointer=saver)

    monkeypatch.setattr(execution, "_graph_cm", _cm)
    limiter = quota.InMemoryQuota()
    monkeypatch.setattr(router, "default_quota", lambda: limiter)
    monkeypatch.setattr(nodes, "SessionLocal", Session)

    prompts = []
    replies = []

    def _chat(messages, **kwargs):
        prompts.append(messages)
        return replies.pop(0) if replies else "assistant reply"

    monkeypatch.setattr(llm, "chat", _chat)
    app.dependency_overrides[get_session] = _get_session
    yield SimpleNamespace(client=TestClient(app), Session=Session, prompts=prompts, replies=replies)
    app.dependency_overrides.clear()


def _sorted_as(monkeypatch, **verdict):
    seen = {}

    def _verdict(message, departments, recent, others=None):
        seen.update(message=message, departments=departments, recent=recent, others=others)
        return {"safety": "none", "route": "private", "action": "none", "steps": "one", **verdict}

    monkeypatch.setattr(guards, "triage_verdict", _verdict)
    return seen


def _send(api, headers, message="hello", thread="t", **extra):
    response = api.client.post("/v1/chat", headers=headers, json={"message": message, "thread_id": thread, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def _history(api, headers, thread="t"):
    return api.client.get(f"/v1/chat/threads/{thread}/messages", headers=headers).json()


def _audit_count(api):
    with api.Session() as s:
        return s.query(AuditLog).filter(AuditLog.action == "request").count()


# ---- private ----
def test_private_message_has_no_department_tag_and_no_audit(api, monkeypatch):
    _sorted_as(monkeypatch, route="private")
    body = _send(api, LEE, "help me write a birthday note")
    assert (body["route"], body["outcome"], body["reply"]) == ("private", "ok", "assistant reply")
    assert _audit_count(api) == 0
    assert _history(api, LEE)[-1]["handled_by"] is None


def test_private_reply_runs_with_no_tools(api, monkeypatch):
    _sorted_as(monkeypatch, route="private")
    _send(api, LEE)
    assert "no access to company data" in api.prompts[-1][0]["content"]


# ---- work ----
def test_work_runs_in_the_department_with_its_tools_and_audit(api, monkeypatch):
    _sorted_as(monkeypatch, route="work", department="logistics")
    monkeypatch.setitem(tools.TOOLS, "track_shipment", lambda state, shipment_id: "in transit")
    api.replies.extend([json.dumps({"tool": "track_shipment", "args": {"shipment_id": "S1"}}), "It is in transit."])

    body = _send(api, LEE, "where is shipment S1?")

    assert (body["route"], body["department"], body["reply"]) == ("work", "logistics", "It is in transit.")
    assert _audit_count(api) == 1


def test_work_appears_in_the_encrypted_chat_and_is_tagged(api, monkeypatch):
    _sorted_as(monkeypatch, route="work", department="logistics")
    _send(api, LEE, "where is shipment S1?")

    [asked, answered] = _history(api, LEE)
    assert asked["content"] == "where is shipment S1?"
    assert answered["handled_by"] == "logistics" and answered["role"] == "assistant"
    assert api.client.get("/v1/chat/threads", headers=LEE).json()[0]["thread_id"] == "t"


def test_private_text_never_reaches_a_work_run(api, monkeypatch):
    _sorted_as(monkeypatch, route="private")
    _send(api, LEE, "PRIVATE WORRY")
    _sorted_as(monkeypatch, route="work", department="logistics")

    _send(api, LEE, "check the stock")

    assert not any("PRIVATE WORRY" in m["content"] for m in api.prompts[-1])


def test_work_is_available_to_later_private_messages_and_triage(api, monkeypatch):
    _sorted_as(monkeypatch, route="work", department="logistics")
    _send(api, LEE, "where is shipment S1?")
    seen = _sorted_as(monkeypatch, route="private")

    _send(api, LEE, "explain that in simple words")

    assert any("assistant reply" == m["content"] for m in api.prompts[-1])
    assert seen["recent"][-1]["content"] == "assistant reply"


def test_internal_fields_are_stripped_before_the_model_server(monkeypatch):
    calls = {}

    class Fake:
        def create(self, **params):
            calls.update(params)
            return SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(content="x"))])

    monkeypatch.setattr("app.llm.client._client", SimpleNamespace(chat=SimpleNamespace(completions=Fake())))
    llm.chat([{"role": "assistant", "content": "hi", "handled_by": "hr"}])
    assert calls["messages"] == [{"role": "assistant", "content": "hi"}]


# ---- requests in a chat ----
def test_order_goes_to_accounts_and_the_requester_can_follow_it(api, monkeypatch):
    _sorted_as(monkeypatch, route="work", action="order")
    body = _send(api, LEE, "get us five laptops")
    assert (body["outcome"], body["department"]) == ("forwarded", "account")
    assert "accounts team" in body["reply"]

    [item] = api.client.get("/v1/chat/threads/t/requests", headers=LEE).json()
    assert (item["kind"], item["label"], item["state"]) == ("task", "order", "queued")

    _sorted_as(monkeypatch, route="private")
    api.client.post(f"/v1/inbox/{body['task_id']}/process", headers=ADMIN)
    [item] = api.client.get("/v1/chat/threads/t/requests", headers=LEE).json()
    assert (item["state"], item["result"]) == ("done", "assistant reply")


def _approval_flow(api, monkeypatch, headers=None):
    headers = headers or _auth("alice", "account")
    _sorted_as(monkeypatch, route="work", department="account")
    monkeypatch.setitem(tools.TOOLS, "manage_bill", lambda state, bill_id: "ok")
    api.replies.append(json.dumps({"tool": "manage_bill", "args": {"bill_id": "B1"}}))
    return headers, _send(api, headers, "record bill B1")


def test_approval_is_followed_and_its_result_reaches_the_requester(api, monkeypatch):
    headers, body = _approval_flow(api, monkeypatch)
    assert body["outcome"] == "pending_approval" and "needs approval" in body["reply"]

    [item] = api.client.get("/v1/chat/threads/t/requests", headers=headers).json()
    assert (item["kind"], item["label"], item["state"]) == ("approval", "manage_bill", "pending")

    api.replies.append("Bill B1 recorded.")
    api.client.post(f"/v1/approvals/{body['approval_id']}/approve", headers=ADMIN)

    [item] = api.client.get("/v1/chat/threads/t/requests", headers=headers).json()
    assert (item["state"], item["result"]) == ("approved", "Bill B1 recorded.")


def test_requests_belong_to_their_owner(api, monkeypatch):
    _approval_flow(api, monkeypatch)
    assert api.client.get("/v1/chat/threads/t/requests", headers=LEE).json() == []


def test_chat_carries_on_while_work_waits_for_approval(api, monkeypatch):
    headers, first = _approval_flow(api, monkeypatch)

    _sorted_as(monkeypatch, route="private")
    assert _send(api, headers, "tell me a joke")["outcome"] == "ok"

    _sorted_as(monkeypatch, route="work", department="account")
    second = _send(api, headers, "show the ledger summary")
    assert second["outcome"] == "ok"

    # the paused run is intact and can still be approved
    api.replies.append("Bill B1 recorded.")
    done = api.client.post(f"/v1/approvals/{first['approval_id']}/approve", headers=ADMIN).json()
    assert (done["outcome"], done["reply"]) == ("ok", "Bill B1 recorded.")


# ---- flagged ----
def test_flagged_message_is_reported_and_not_kept_on_any_route(api, monkeypatch):
    _sorted_as(monkeypatch, safety="illegal", route="work", department="logistics")
    body = _send(api, LEE, "BAD MESSAGE")
    assert body["outcome"] == "flagged_nsfw" and body["reply"] == ""
    assert api.prompts == []
    with api.Session() as s:
        assert [r.message for r in s.query(Report).all()] == ["BAD MESSAGE"]
    assert _history(api, LEE) == []


# ---- admin finance notices keep working through the chat box ----
def test_admin_finance_work_notifies_accounts_but_private_finance_talk_does_not(api, monkeypatch):
    _sorted_as(monkeypatch, route="private")
    _send(api, ADMIN, "my personal taxes worry me")
    with api.Session() as s:
        assert s.query(Report).count() == 0

    _sorted_as(monkeypatch, route="work", department="account")
    _send(api, ADMIN, "show the ACME balance")
    with api.Session() as s:
        assert [r.kind for r in s.query(Report).all()] == ["finance_notice"]


# ---- departments ----
def test_triage_only_offers_the_users_own_departments(api, monkeypatch):
    seen = _sorted_as(monkeypatch)
    _send(api, MULTI)
    assert set(seen["departments"]) == {"logistics", "researcher"}
    _send(api, ADMIN)
    assert {"account", "hr", "logistics", "researcher", "superuser", "admin"} == set(seen["departments"])


@pytest.mark.parametrize("department", ["logistics", "researcher"])
def test_profile_with_several_departments_can_work_in_each(api, monkeypatch, department):
    _sorted_as(monkeypatch, route="work", department=department)
    body = _send(api, MULTI, "do the work")
    assert (body["outcome"], body["department"]) == ("ok", department)


def test_department_outside_the_profile_is_refused_even_if_the_model_picks_it(api, monkeypatch):
    _sorted_as(monkeypatch, route="work", department="account")
    body = _send(api, LEE, "show the balance")
    assert body["outcome"] == "denied_auth" and "isn't part of" in body["reply"]
    assert api.prompts == []


def test_person_with_no_department_always_stays_private(api, monkeypatch):
    _sorted_as(monkeypatch, route="work", department="logistics")
    assert _send(api, NOBODY, "track it")["route"] == "private"


# ---- triage parsing and defaults ----
def _departments():
    return {"logistics": "Logistics"}


@pytest.mark.parametrize(
    "reply",
    ["no json here", "{not json}", "[1]", '{"safety": "maybe", "route": "work"}', '{"safety": "none", "route": "x"}'],
)
def test_unusable_verdict_is_none(monkeypatch, reply):
    monkeypatch.setattr(llm, "chat", lambda messages, **k: reply)
    assert triage_verdict("x", _departments(), []) is None


def test_verdict_is_none_when_the_model_is_down(monkeypatch):
    def _down(messages, **k):
        raise ConnectionError

    monkeypatch.setattr(llm, "chat", _down)
    assert triage_verdict("x", _departments(), []) is None


def test_prompt_lists_departments_and_recent_conversation(monkeypatch):
    prompts = []

    def _chat(messages, **k):
        prompts.append(messages)
        return '{"safety": "none", "route": "private"}'

    monkeypatch.setattr(llm, "chat", _chat)
    triage_verdict("and for S2?", _departments(), [{"role": "user", "content": "track S1"}])
    assert "- logistics: Logistics" in prompts[0][0]["content"]
    assert "user: track S1" in prompts[0][0]["content"]
    assert prompts[0][1] == {"role": "user", "content": "and for S2?"}


def test_no_verdict_stays_private_and_keywords_still_flag(monkeypatch):
    assert guards.triage("track shipment", _departments(), []).route == "private"
    assert guards.triage("how to launder money", _departments(), []).nsfw is True


@pytest.mark.parametrize(
    ("verdict", "route", "department", "action"),
    [
        ({"route": "private", "department": "logistics"}, "private", None, None),
        ({"route": "work", "department": None}, "private", None, None),
        ({"route": "work", "department": "logistics"}, "work", "logistics", None),
        ({"route": "private", "action": "bill"}, "work", None, "bill"),
        ({"route": "work", "department": "logistics", "action": "weird"}, "work", "logistics", None),
    ],
)
def test_triage_decisions(monkeypatch, verdict, route, department, action):
    monkeypatch.setattr(
        guards, "triage_verdict", lambda *a: {"safety": "none", **verdict}
    )
    result = guards.triage("x", _departments(), [])
    assert (result.route, result.department, result.action) == (route, department, action)


def test_many_steps_choose_agents_mode_unless_the_caller_set_one(api, monkeypatch):
    _sorted_as(monkeypatch, steps="many")
    assert _send(api, LEE)["mode"] == "agents"
    assert _send(api, LEE, thread="u", mode="single")["mode"] == "single"


# ---- profile and departments API ----
def _make_user(api, name="alice", roles=("hr",)):
    return api.client.post(
        "/v1/auth/users", headers=ADMIN, json={"username": name, "password": "x" * 12, "roles": list(roles)}
    ).json()


def test_profile_shows_departments_and_role(api):
    user = _make_user(api, roles=("hr", "logistics"))
    token = issue_token(user["id"], "alice", user["roles"])
    me = api.client.get("/v1/auth/me", headers={"authorization": f"Bearer {token}"}).json()
    assert me["username"] == "alice" and me["departments"] == ["hr", "logistics"]
    assert me["is_admin"] is False and me["chat_enabled"] is False


def test_admin_can_assign_several_departments(api):
    user = _make_user(api)
    url = f"/v1/auth/users/{user['id']}/departments"
    updated = api.client.put(url, headers=ADMIN, json={"departments": ["logistics", "account"]}).json()
    assert updated["roles"] == ["account", "logistics"]
    assert api.client.put(url, headers=ADMIN, json={"departments": []}).json()["roles"] == []


def test_department_changes_are_checked(api):
    user = _make_user(api)
    url = f"/v1/auth/users/{user['id']}/departments"
    assert api.client.put(url, headers=ADMIN, json={"departments": ["nope"]}).status_code == 400
    assert api.client.put(url, headers=ADMIN, json={"departments": ["admin"]}).status_code == 400
    assert api.client.put(url, headers=LEE, json={"departments": ["hr"]}).status_code == 403
    assert api.client.put("/v1/auth/users/none/departments", headers=ADMIN, json={"departments": []}).status_code == 404


def test_changing_departments_keeps_the_admin_role(api):
    user = _make_user(api, "boss", ("admin",))
    url = f"/v1/auth/users/{user['id']}/departments"
    assert api.client.put(url, headers=ADMIN, json={"departments": ["hr"]}).json()["roles"] == ["admin", "hr"]


def test_work_for_a_department_you_are_not_in_is_refused_not_rerouted(api, monkeypatch):
    """Triage names the real owner; the Request Manager refuses instead of a wrong department guessing."""
    seen = _sorted_as(monkeypatch, route="work", department="account")
    out = _send(api, LEE, "pull up the balance sheet")
    assert "account" in seen["others"] and "account" not in seen["departments"]
    assert out["route"] == "work" and out["outcome"] == "denied_auth"
