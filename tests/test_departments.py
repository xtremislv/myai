"""Department packs: seeding, idempotency, and the ACL behavior they produce.

Uses in-memory SQLite so it runs without Postgres.
"""
from __future__ import annotations

import base64
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.acl.engine import ACLEngine
from app.acl.models import Action, Resource, Subject
from app.db.session import ACLPolicy, Base, Department
from app.departments.packs import DEFAULT_DEPARTMENTS
from app.departments.registry import get_department, seed_departments


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", base64.b64encode(os.urandom(32)).decode())
    from app import config

    config.get_settings.cache_clear()
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as s:
        yield s


def _subject(*roles: str) -> Subject:
    return Subject(id="u1", roles=tuple(roles))


def test_seed_creates_departments_and_policies(session):
    summary = seed_departments(session)
    assert summary["departments"] == len(DEFAULT_DEPARTMENTS)
    assert summary["policies_added"] > 0
    assert session.query(Department).count() == len(DEFAULT_DEPARTMENTS)
    # metadata persisted
    fin = get_department(session, "finance")
    assert fin.requires_approval is True
    assert "secret:payment/*" in fin.critical_resources
    assert "initiate_payment" in fin.tools


def test_seed_is_idempotent(session):
    seed_departments(session)
    first = session.query(ACLPolicy).count()
    second_summary = seed_departments(session)
    assert second_summary["policies_added"] == 0
    assert session.query(ACLPolicy).count() == first


def test_finance_scope(session):
    seed_departments(session)
    eng = ACLEngine(session)
    fin = _subject("finance")
    # allowed inside its own data
    assert eng.check(fin, Action.READ, Resource("object", "finance/ledger.csv")).allowed
    assert eng.check(fin, Action.WRITE, Resource("object", "invoices/2026-10.pdf")).allowed
    # payment secrets: read allowed (approval handled by the RM), write denied
    assert eng.check(fin, Action.READ, Resource("secret", "payment/stripe")).allowed
    assert not eng.check(fin, Action.WRITE, Resource("secret", "payment/stripe")).allowed
    # cannot cross into logistics data
    assert not eng.check(fin, Action.READ, Resource("object", "inventory/skus.csv")).allowed


def test_logistics_and_research_scopes(session):
    seed_departments(session)
    eng = ACLEngine(session)
    log = _subject("logistics")
    res = _subject("research")
    assert eng.check(log, Action.WRITE, Resource("object", "inventory/skus.csv")).allowed
    assert not eng.check(log, Action.READ, Resource("object", "finance/ledger.csv")).allowed
    # research may read reports but not write them
    assert eng.check(res, Action.READ, Resource("object", "reports/q3.pdf")).allowed
    assert not eng.check(res, Action.WRITE, Resource("object", "reports/q3.pdf")).allowed
    assert eng.check(res, Action.WRITE, Resource("object", "research/notes.md")).allowed


def test_admin_full_access(session):
    seed_departments(session)
    eng = ACLEngine(session)
    adm = _subject("admin")
    assert eng.check(adm, Action.ADMIN, Resource("system", "*")).allowed
    assert eng.check(adm, Action.DELETE, Resource("object", "anything/at/all")).allowed
    assert eng.check(adm, Action.READ, Resource("secret", "payment/stripe")).allowed


def test_unknown_role_denied(session):
    seed_departments(session)
    eng = ACLEngine(session)
    nobody = _subject("intern")
    assert not eng.check(nobody, Action.READ, Resource("object", "shared/readme.txt")).allowed
