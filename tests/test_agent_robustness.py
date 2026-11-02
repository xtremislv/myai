"""Team follow-up turns, bad tool arguments, quota failover, and mail resources."""
from __future__ import annotations

import json

import pytest
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.acl.engine import ACLEngine
from app.acl.models import Action, Resource, Subject
from app.db.session import Base
from app.departments.registry import seed_departments
from app.llm import client as llm
from app.orchestrator import tools
from app.orchestrator.graph import build_graph
from app.orchestrator.multi_graph import build_team_graph
from app.orchestrator.nodes import run_tool
from app.request_manager.quota import FailoverQuota, InMemoryQuota


def _state(department):
    return {"subject_id": "alice", "subject_roles": [department], "scratchpad": {"department": department}}


# ---- team follow-ups ----
def test_team_answers_the_follow_up_with_earlier_context(monkeypatch):
    prompts = {"planner": [], "researcher": [], "writer": []}

    def _chat(messages, **kwargs):
        system = messages[0]["content"]
        if system.startswith("You are the PLANNER"):
            prompts["planner"].append(messages)
            return "1. answer it"
        if system.startswith("You are the RESEARCHER"):
            prompts["researcher"].append(messages)
            return json.dumps({"done": True})
        prompts["writer"].append(messages)
        return "FINAL ANSWER"

    monkeypatch.setattr(llm, "chat", _chat)
    graph = build_team_graph().compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "t"}}
    base = _state("researcher")

    graph.invoke({**base, "messages": [{"role": "user", "content": "FIRST QUESTION"}]}, config=config)
    graph.invoke({**base, "messages": [{"role": "user", "content": "SECOND QUESTION"}]}, config=config)

    for agent in ("planner", "researcher", "writer"):
        convo = prompts[agent][-1]
        assert convo[-1] == {"role": "user", "content": "SECOND QUESTION"}, agent
        assert any(m["content"] == "FIRST QUESTION" for m in convo), agent
        assert any(m["content"] == "FINAL ANSWER" for m in convo), agent
        # Internal plan entries appear only as the single current-plan message.
        assert not any(m["role"] == "tool" for m in convo), agent


# ---- tool argument handling ----
@pytest.mark.parametrize("args", [{"wrong": 1}, {}, "not-an-object", None, {"state": "x", "shipment_id": "1"}])
def test_bad_arguments_return_an_error_to_the_agent(args):
    out = run_tool(_state("logistics"), "track_shipment", args)
    assert out.startswith("ERROR: invalid arguments") and "shipment_id" in out


def test_graph_survives_a_bad_tool_call_and_the_agent_sees_why(monkeypatch):
    replies = [json.dumps({"tool": "track_shipment", "args": {"wrong": 1}}), "Sorry, let me fix that."]
    prompts = []

    def _chat(messages, **kwargs):
        prompts.append(messages)
        return replies.pop(0)

    monkeypatch.setattr(llm, "chat", _chat)
    graph = build_graph().compile(checkpointer=MemorySaver())
    final = graph.invoke(
        {**_state("logistics"), "messages": [{"role": "user", "content": "track it"}]},
        config={"configurable": {"thread_id": "t"}},
    )
    assert final["messages"][-1]["content"] == "Sorry, let me fix that."
    assert "invalid arguments" in prompts[-1][-1]["content"]


def test_failing_tool_does_not_kill_the_run_or_leak_details(monkeypatch):
    def _boom(state, shipment_id):
        raise RuntimeError("postgres password is hunter2")

    monkeypatch.setitem(tools.TOOLS, "track_shipment", _boom)
    out = run_tool(_state("logistics"), "track_shipment", {"shipment_id": "1"})
    assert out == "ERROR: tool 'track_shipment' failed"


def test_tool_outside_the_department_is_refused():
    assert "not available" in run_tool(_state("hr"), "track_shipment", {"shipment_id": "1"})


# ---- quota failover ----
class _DownRedis:
    def allow(self, department, limit_per_min):
        raise ConnectionError("redis is down")


def test_quota_still_limits_when_redis_is_down():
    quota = FailoverQuota(_DownRedis(), InMemoryQuota())
    assert [quota.allow("hr", 2) for _ in range(3)] == [True, True, False]


def test_quota_prefers_the_primary():
    primary, fallback = InMemoryQuota(), InMemoryQuota()
    quota = FailoverQuota(primary, fallback)
    quota.allow("hr", 5)
    assert fallback._win == {} and primary._win


def test_default_quota_is_shared_between_requests():
    from app.request_manager.quota import default_quota

    assert default_quota() is default_quota()


# ---- read_mail resources must match the ACL grants ----
@pytest.fixture
def real_acl(monkeypatch):
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as s:
        seed_departments(s)

    def _enforce(state, action, rtype, rid):
        with Session() as s:
            subject = Subject(state["subject_id"], tuple(state["subject_roles"]))
            decision = ACLEngine(s).check(subject, Action(action), Resource(rtype, rid))
            return None if decision.allowed else f"ACCESS DENIED: {decision.reason}"

    monkeypatch.setattr(tools, "_enforce", _enforce)


def _read_mail(department, *args, subject="alice"):
    state = {"subject_id": subject, "subject_roles": [department]}
    return tools.read_mail(state, *args)


def test_read_mail_default_box_is_allowed_for_the_owning_department(real_acl):
    assert not _read_mail("account", "finance").startswith("ACCESS DENIED")
    assert not _read_mail("account", "finance", "archive").startswith("ACCESS DENIED")
    assert not _read_mail("hr", "career").startswith("ACCESS DENIED")


def test_read_mail_other_departments_are_denied(real_acl):
    assert _read_mail("hr", "finance").startswith("ACCESS DENIED")
    assert _read_mail("account", "career").startswith("ACCESS DENIED")


def test_personal_mail_resolves_to_the_caller_only(real_acl):
    assert not _read_mail("hr", "personal").startswith("ACCESS DENIED")
    # Asking for someone else's box just stays inside the caller's own mailbox.
    resource = []
    original = tools._enforce

    def _capture(state, action, rtype, rid):
        resource.append(rid)
        return original(state, action, rtype, rid)

    tools._enforce = _capture
    try:
        _read_mail("hr", "personal", "bob/inbox")
    finally:
        tools._enforce = original
    assert resource == ["personal/alice/bob/inbox"]
