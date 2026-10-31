"""Request Manager pipeline: classification, authorization, quota, critical
approval gate, and approved execution. No live LLM/Redis/Postgres — a fake
runner and in-memory quota are injected, SQLite backs the store.
"""
from __future__ import annotations

import base64
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.acl.models import Subject
from app.db.session import Approval, Base
from app.departments.registry import seed_departments
from app.request_manager import approvals as approvals_store
from app.request_manager.classifier import classify
from app.request_manager.quota import InMemoryQuota
from app.request_manager.router import RMRequest, execute_approved, handle_request


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


# ---- classifier ----
def test_classify_sole_role(session):
    c = classify(Subject("u", ("logistics",)), "track my shipment")
    assert c.department == "logistics"


def test_classify_keyword_among_multiple_roles(session):
    c = classify(Subject("u", ("finance", "research")), "please summarize this report")
    assert c.department == "research"


def test_classify_explicit_requires_membership(session):
    assert classify(Subject("u", ("research",)), "x", explicit="finance").department is None
    assert classify(Subject("u", ("finance",)), "x", explicit="finance").department == "finance"


# ---- pipeline ----
def test_happy_path_executes(session):
    subj = Subject("u", ("logistics",))
    res = handle_request(session, subj, RMRequest("track SHP-1", "t1"), quota=InMemoryQuota(), runner=_runner)
    assert res.outcome == "ok"
    assert res.department == "logistics"
    assert "handled" in res.reply


def test_unroutable_denied(session):
    subj = Subject("u", ())  # no department roles
    res = handle_request(session, subj, RMRequest("do something", "t1"), quota=InMemoryQuota(), runner=_runner)
    assert res.outcome == "denied_auth"


def test_quota_enforced(session):
    subj = Subject("u", ("logistics",))  # quota_per_min = 120 for logistics
    q = InMemoryQuota()

    # Force a tiny limit by exhausting the window via a department with low quota.
    fin = Subject("f", ("finance",))  # finance quota_per_min = 30, but requires approval -> use a non-critical msg
    # Non-critical finance message to avoid the approval branch.
    for _ in range(30):
        r = handle_request(session, fin, RMRequest("show invoice list", "t"), quota=q, runner=_runner)
        assert r.outcome == "ok"
    r = handle_request(session, fin, RMRequest("show invoice list", "t"), quota=q, runner=_runner)
    assert r.outcome == "denied_quota"


def test_critical_request_is_gated_then_approved(session):
    subj = Subject("alice", ("finance",))
    # Critical keyword 'payment' -> parked, not executed.
    res = handle_request(
        session, subj, RMRequest("initiate a payment to ACME for 5000", "t9"),
        quota=InMemoryQuota(), runner=_runner,
    )
    assert res.outcome == "pending_approval"
    assert res.approval_id

    row = session.get(Approval, res.approval_id)
    assert row.state == "pending"

    # Admin approves -> execute.
    decided = approvals_store.decide(session, res.approval_id, approve=True, decided_by="admin-1")
    assert decided.state == "approved"
    reply = execute_approved(session, decided, runner=_runner)
    assert "handled" in reply


def test_critical_request_can_be_denied(session):
    subj = Subject("alice", ("finance",))
    res = handle_request(
        session, subj, RMRequest("wire transfer 10000 to vendor", "t10"),
        quota=InMemoryQuota(), runner=_runner,
    )
    assert res.outcome == "pending_approval"
    decided = approvals_store.decide(session, res.approval_id, approve=False, decided_by="admin-1", reason="too large")
    assert decided.state == "denied"
    assert approvals_store.list_pending(session) == []
