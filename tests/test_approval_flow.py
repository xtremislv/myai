"""Thread isolation and in-graph approvals, through the real graphs and the Request
Manager. The LLM is faked and checkpoints live in memory; no Postgres or GPU."""
from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.acl.models import Subject
from app.db.session import Approval, Base, DepartmentTask
from app.departments.packs import critical_tools
from app.departments.registry import seed_departments
from app.llm import client as llm
from app.orchestrator import execution, tools
from app.orchestrator.graph import build_graph
from app.orchestrator.multi_graph import build_team_graph
from app.request_manager import approvals as store
from app.request_manager.quota import InMemoryQuota
from app.request_manager.router import (
    RMRequest,
    execute_task,
    handle_request,
    resolve_approval,
)

BILL_CALL = json.dumps({"tool": "manage_bill", "args": {"bill_id": "B1", "detail": "ACME"}})


@pytest.fixture
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, future=True)() as s:
        seed_departments(s)
        yield s


@pytest.fixture
def graphs(monkeypatch):
    """Back both graphs with one in-memory checkpointer."""
    saver = MemorySaver()

    @contextmanager
    def _cm(mode, serde=None):
        builder = build_team_graph() if mode == "team" else build_graph()
        yield builder.compile(checkpointer=saver)

    monkeypatch.setattr(execution, "_graph_cm", _cm)


@pytest.fixture
def executed(monkeypatch):
    """Replace the critical tools with recorders: the list holds real executions."""
    calls = []

    def _manage_bill(state, bill_id="new", detail=""):
        calls.append(("manage_bill", bill_id, detail))
        return "bill recorded"

    def _initiate_payment(state, payee, amount):
        calls.append(("initiate_payment", payee, amount))
        return "paid"

    monkeypatch.setitem(tools.TOOLS, "manage_bill", _manage_bill)
    monkeypatch.setitem(tools.TOOLS, "initiate_payment", _initiate_payment)
    return calls


def _script(monkeypatch, *replies):
    """The LLM returns these replies in order, and records each prompt."""
    prompts = []
    queue = list(replies)

    def _chat(messages, **kwargs):
        prompts.append(messages)
        return queue.pop(0)

    monkeypatch.setattr(llm, "chat", _chat)
    return prompts


def _request(session, subject, message, thread="t1", mode="single"):
    # "ledger" style wording stays clear of the forwarding keywords.
    return handle_request(session, subject, RMRequest(message, thread, mode), quota=InMemoryQuota())


def _decide(session, approval_id, *, approve):
    row, won = store.decide(session, approval_id, approve=approve, decided_by="admin-1")
    assert won
    return resolve_approval(session, row)


ALICE = Subject("alice", ("account",))


# ---- thread isolation ----
def test_same_thread_id_does_not_share_conversation_between_users(session, graphs, monkeypatch):
    prompts = _script(monkeypatch, "ok", "ok")
    _request(session, Subject("alice", ("hr",)), "ALICE SECRET", thread="shared")
    _request(session, Subject("bob", ("hr",)), "what did alice say?", thread="shared")
    assert not any("ALICE SECRET" in m["content"] for m in prompts[1])


def test_same_user_keeps_context_on_a_thread(session, graphs, monkeypatch):
    prompts = _script(monkeypatch, "ok", "ok")
    subject = Subject("alice", ("hr",))
    _request(session, subject, "FIRST", thread="mine")
    _request(session, subject, "SECOND", thread="mine")
    assert any("FIRST" in m["content"] for m in prompts[1])


# ---- in-graph approval ----
@pytest.mark.parametrize("mode", ["single", "team"])
def test_critical_tool_waits_for_approval_then_runs_once(session, graphs, executed, monkeypatch, mode):
    if mode == "single":
        replies = [BILL_CALL, "Bill B1 recorded."]
    else:
        # planner, researcher's call, researcher done, reviewer's verdict, writer
        replies = ["1. record the bill", BILL_CALL, "I have what the plan needs.", json.dumps({"enough": True}),
                   "Bill B1 recorded."]
    _script(monkeypatch, *replies)

    res = _request(session, ALICE, "record the ACME ledger entry", mode=mode)

    assert res.outcome == "pending_approval"
    assert executed == []
    row = session.get(Approval, res.approval_id)
    assert (row.tool, row.tool_args) == ("manage_bill", {"bill_id": "B1", "detail": "ACME"})

    done = _decide(session, res.approval_id, approve=True)
    assert done.outcome == "ok" and done.reply == "Bill B1 recorded."
    assert executed == [("manage_bill", "B1", "ACME")]


def test_benign_message_with_a_trigger_word_is_not_gated(session, graphs, executed, monkeypatch):
    _script(monkeypatch, "Payroll closes on Friday.")
    res = _request(session, ALICE, "when does payroll close?")
    assert res.outcome == "ok"


def test_critical_action_without_a_trigger_word_is_gated(session, graphs, executed, monkeypatch):
    _script(monkeypatch, json.dumps({"tool": "initiate_payment", "args": {"payee": "ACME", "amount": 5}}))
    res = _request(session, ALICE, "settle up with ACME")
    assert res.outcome == "pending_approval"
    assert executed == []


def test_denied_tool_never_runs_and_agent_is_told(session, graphs, executed, monkeypatch):
    prompts = _script(monkeypatch, BILL_CALL, "That was rejected.")
    res = _request(session, ALICE, "record the ACME ledger entry")

    done = _decide(session, res.approval_id, approve=False)

    assert done.outcome == "denied" and done.reply == "That was rejected."
    assert executed == []
    assert any("DENIED" in m["content"] for m in prompts[-1])


def test_second_critical_call_needs_its_own_approval(session, graphs, executed, monkeypatch):
    pay = json.dumps({"tool": "initiate_payment", "args": {"payee": "ACME", "amount": 5}})
    _script(monkeypatch, BILL_CALL, pay, "All done.")
    first = _request(session, ALICE, "record the ACME ledger entry and settle it")

    second = _decide(session, first.approval_id, approve=True)
    assert second.outcome == "pending_approval"
    assert executed == [("manage_bill", "B1", "ACME")]

    final = _decide(session, second.approval_id, approve=True)
    assert final.outcome == "ok"
    assert executed[-1] == ("initiate_payment", "ACME", 5)


def test_second_decision_loses(session, graphs, executed, monkeypatch):
    _script(monkeypatch, BILL_CALL, "done")
    res = _request(session, ALICE, "record the ACME ledger entry")
    assert _decide(session, res.approval_id, approve=True).outcome == "ok"
    _, won = store.decide(session, res.approval_id, approve=True, decided_by="admin-2")
    assert not won
    assert len(executed) == 1


def test_new_message_on_a_paused_thread_keeps_the_pause(session, graphs, executed, monkeypatch):
    _script(monkeypatch, BILL_CALL, "done")
    res = _request(session, ALICE, "record the ACME ledger entry")

    again = _request(session, ALICE, "also do something else")
    assert again.outcome == "pending_approval" and again.approval_id == res.approval_id

    assert _decide(session, res.approval_id, approve=True).outcome == "ok"
    assert len(executed) == 1


def test_resume_refuses_a_run_paused_on_a_different_call(session, graphs, executed, monkeypatch):
    _script(monkeypatch, BILL_CALL)
    res = _request(session, ALICE, "record the ACME ledger entry")
    row = session.get(Approval, res.approval_id)
    row.tool_args = {"bill_id": "B2", "detail": "other"}
    session.commit()

    done = _decide(session, res.approval_id, approve=True)

    assert done.outcome == "error"
    assert session.get(Approval, res.approval_id).state == "failed"
    assert executed == []


def test_admin_tool_access_does_not_skip_the_gate(session, graphs, executed, monkeypatch):
    _script(monkeypatch, BILL_CALL)
    res = handle_request(
        session, Subject("root", ("admin",)), RMRequest("record it", "t1", department="admin"),
        quota=InMemoryQuota(),
    )
    assert res.outcome == "pending_approval"
    assert executed == []


@pytest.mark.parametrize(
    ("tool", "args"),
    [("manage_order", {"order_id": "O1"}), ("book_service", {"service": "courier"})],
)
def test_orders_and_bookings_need_approval(session, graphs, monkeypatch, tool, args):
    ran = []
    monkeypatch.setitem(tools.TOOLS, tool, lambda state, **kw: ran.append(kw) or "ok")
    _script(monkeypatch, json.dumps({"tool": tool, "args": args}), "Done.")

    res = _request(session, ALICE, "handle the ACME ledger entry")

    assert res.outcome == "pending_approval" and ran == []
    assert _decide(session, res.approval_id, approve=True).outcome == "ok"
    assert ran == [args]


def test_every_critical_tool_exists():
    assert critical_tools() == {
        "manage_bill", "initiate_payment", "manage_order", "book_service", "send_mail", "browser_commit",
        "delete_file",
    }
    assert critical_tools() <= set(tools.TOOLS)


# ---- the inbox route used to bypass approval ----
def test_forwarded_payment_cannot_skip_approval(session, graphs, executed, monkeypatch):
    forwarded = _request(session, Subject("u1", ("logistics",)), "pay the ACME invoice now")
    assert forwarded.outcome == "forwarded"
    task = session.get(DepartmentTask, forwarded.task_id)

    _script(monkeypatch, BILL_CALL, "Bill recorded.")
    res = execute_task(session, task, "clerk")

    assert res.outcome == "pending_approval"
    assert executed == []
    assert task.state == "awaiting"

    done = _decide(session, res.approval_id, approve=True)
    assert done.outcome == "ok"
    assert len(executed) == 1
    assert task.state == "done" and task.result == "Bill recorded."


def test_inbox_task_uses_its_own_thread(session, graphs, monkeypatch):
    forwarded = _request(session, Subject("u1", ("logistics",)), "place an order for laptops", thread="clerk-chat")
    task = session.get(DepartmentTask, forwarded.task_id)
    prompts = _script(monkeypatch, "private chat reply", "order handled")
    _request(session, Subject("clerk", ("account",)), "MY OWN NOTE", thread="clerk-chat")

    assert execute_task(session, task, "clerk").outcome == "ok"
    assert not any("MY OWN NOTE" in m["content"] for m in prompts[-1])
