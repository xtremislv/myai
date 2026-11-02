"""Request Manager pipeline: NSFW guard, order forwarding, classification,
authorization, quota, and the critical-approval gate. Fake runner + in-memory
quota injected; SQLite backs the store."""
from __future__ import annotations

import base64
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.acl.models import Subject
from app.db.session import Approval, Base, Department, DepartmentTask, Report
from app.departments.registry import seed_departments
from app.orchestrator.execution import PendingAction
from app.request_manager import approvals as approvals_store
from app.request_manager.classifier import classify
from app.request_manager.quota import InMemoryQuota
from app.request_manager.router import RMRequest, handle_request, resolve_approval


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", base64.b64encode(os.urandom(32)).decode())
    from app import config

    config.get_settings.cache_clear()
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as s:
        seed_departments(s)
        yield s


def _runner(subject, req, department):
    return f"[{department}] handled: {req.message}"


def _handle(session, subj, msg, thread="t1"):
    return handle_request(session, subj, RMRequest(msg, thread), quota=InMemoryQuota(), runner=_runner)


# ---- classifier ----
def test_classify_sole_role(session):
    assert classify(Subject("u", ("logistics",)), "track my shipment").department == "logistics"


def test_classify_keyword_among_roles(session):
    assert classify(Subject("u", ("account", "researcher")), "summarize this report").department == "researcher"


def test_classify_explicit_requires_membership(session):
    assert classify(Subject("u", ("researcher",)), "x", explicit="account").department is None
    assert classify(Subject("u", ("account",)), "x", explicit="account").department == "account"


# ---- NSFW guard ----
def test_nsfw_is_flagged_and_reported(session):
    res = _handle(session, Subject("u", ("researcher",)), "this is an nsfw request please help")
    assert res.outcome == "flagged_nsfw"
    assert res.report_id
    rep = session.get(Report, res.report_id)
    assert rep.kind == "nsfw" and set(rep.notified) == {"hr", "admin"}


# ---- order/bill forwarding ----
def test_order_request_forwarded_to_account(session):
    # requester is NOT in accounts; order still forwards to the accounts inbox
    res = _handle(session, Subject("u", ("researcher",)), "please place an order for 10 laptops")
    assert res.outcome == "forwarded"
    assert res.department == "account"
    task = session.get(DepartmentTask, res.task_id)
    assert task.department == "account" and task.kind == "order" and task.state == "queued"


def test_bill_forwarded_to_account(session):
    res = _handle(session, Subject("u", ("logistics",)), "pay the invoice from the vendor")
    assert res.outcome == "forwarded" and res.department == "account"


# ---- pipeline ----
def test_happy_path_executes(session):
    res = _handle(session, Subject("u", ("logistics",)), "track shipment SHP-1")
    assert res.outcome == "ok" and res.department == "logistics" and "handled" in res.reply


def test_unroutable_denied(session):
    res = _handle(session, Subject("u", ()), "do something vague")
    assert res.outcome == "denied_auth"


def test_quota_enforced(session):
    # shrink account quota for the test
    session.get(Department, "account").quota_per_min = 3
    session.commit()
    subj = Subject("u", ("account",))
    q = InMemoryQuota()
    for _ in range(3):
        r = handle_request(session, subj, RMRequest("show account summary", "t"), quota=q, runner=_runner)
        assert r.outcome == "ok"
    r = handle_request(session, subj, RMRequest("show account summary", "t"), quota=q, runner=_runner)
    assert r.outcome == "denied_quota"


def test_paused_run_is_parked_then_resumed_on_approval(session):
    pending = PendingAction("manage_bill", {"bill_id": "B1"}, "alice:t9")
    subj = Subject("alice", ("account",))
    res = handle_request(
        session, subj, RMRequest("record the ACME ledger entry", "t9"),
        quota=InMemoryQuota(), runner=lambda *a: pending,
    )
    assert res.outcome == "pending_approval" and res.approval_id
    row = session.get(Approval, res.approval_id)
    assert (row.state, row.tool, row.checkpoint_thread) == ("pending", "manage_bill", "alice:t9")

    decided, won = approvals_store.decide(session, res.approval_id, approve=True, decided_by="admin-1")
    assert won and decided.state == "approved"
    done = resolve_approval(session, decided, resumer=lambda approval, approved, reason: "recorded")
    assert done.outcome == "ok" and done.reply == "recorded"
