"""Unit tests for the vault (envelope encryption) and ACL engine, using SQLite
in-memory so they run without Postgres."""
from __future__ import annotations

import base64
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.acl.engine import ACLEngine
from app.acl.models import Action, Resource, Subject
from app.db.session import ACLPolicy, Base
from app.vault.aesgcm_vault import AESGCMVault


@pytest.fixture
def session(monkeypatch):
    # Ensure a master key exists for the vault.
    monkeypatch.setenv("VAULT_MASTER_KEY", base64.b64encode(os.urandom(32)).decode())
    from app import config

    config.get_settings.cache_clear()  # pick up the env var

    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as s:
        yield s


def test_vault_roundtrip(session):
    v = AESGCMVault(session)
    v.put("api_key", "s3cr3t-value")
    assert v.get("api_key") == "s3cr3t-value"
    assert "api_key" in v.list_names()
    v.delete("api_key")
    assert v.get("api_key") is None


def test_vault_rotation(session):
    v = AESGCMVault(session)
    v.put("k", "v")
    new_master = base64.b64encode(os.urandom(32)).decode()
    assert v.rotate_master_key(new_master) == 1
    assert v.get("k") == "v"  # still readable under the new master


def test_acl_deny_by_default(session):
    eng = ACLEngine(session)
    subj = Subject(id="u1", roles=("analyst",))
    d = eng.check(subj, Action.READ, Resource("object", "reports/q3.pdf"))
    assert not d.allowed


def test_acl_allow_by_role_glob(session):
    session.add(
        ACLPolicy(id="p1", subject="analyst", effect="allow", action="read", resource="object:reports/*")
    )
    session.commit()
    eng = ACLEngine(session)
    subj = Subject(id="u1", roles=("analyst",))
    assert eng.check(subj, Action.READ, Resource("object", "reports/q3.pdf")).allowed
    # different prefix -> denied
    assert not eng.check(subj, Action.READ, Resource("object", "secrets/root.key")).allowed


def test_acl_explicit_deny_overrides_allow(session):
    session.add_all(
        [
            ACLPolicy(id="a", subject="analyst", effect="allow", action="*", resource="object:*"),
            ACLPolicy(id="d", subject="analyst", effect="deny", action="read", resource="object:reports/secret"),
        ]
    )
    session.commit()
    eng = ACLEngine(session)
    subj = Subject(id="u1", roles=("analyst",))
    assert eng.check(subj, Action.READ, Resource("object", "reports/public")).allowed
    assert not eng.check(subj, Action.READ, Resource("object", "reports/secret")).allowed
