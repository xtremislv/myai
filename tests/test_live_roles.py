"""A person's departments come from the database on every request, not from their token."""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import deps
from app.api.deps import subject_from_claims  # the real one; conftest patches the attribute
from app.auth.security import issue_token
from app.db.session import Base, User, get_session
from app.departments.registry import seed_departments
from app.llm import client as llm
from app.main import app
from app.orchestrator import execution
from app.orchestrator.graph import build_graph
from app.request_manager import quota, router


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(deps, "subject_from_claims", subject_from_claims)
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        seed_departments(s)
        s.add_all([
            User(id="root", username="root", password_hash="x", roles=["admin"]),
            User(id="sam", username="sam", password_hash="x", roles=["logistics", "researcher"]),
        ])
        s.commit()

    def _get_session():
        with Session() as s:
            yield s

    saver = MemorySaver()

    @contextmanager
    def _cm(mode, serde=None):
        yield build_graph().compile(checkpointer=saver)

    monkeypatch.setattr(execution, "_graph_cm", _cm)
    limiter = quota.InMemoryQuota()
    monkeypatch.setattr(router, "default_quota", lambda: limiter)
    monkeypatch.setattr(llm, "chat", lambda messages, **k: "ok")
    app.dependency_overrides[get_session] = _get_session
    yield SimpleNamespace(client=TestClient(app), Session=Session)
    app.dependency_overrides.clear()


def _token(user, *roles):
    return {"authorization": f"Bearer {issue_token(user, user, list(roles))}"}


ADMIN = _token("root", "admin")
SAM = _token("sam", "logistics", "researcher")


def _work(api, headers, department):
    return api.client.post(
        "/v1/request", headers=headers,
        json={"message": "do the work", "thread_id": "t", "department": department},
    )


def _set_departments(api, *departments):
    response = api.client.put(
        "/v1/auth/users/sam/departments", headers=ADMIN, json={"departments": list(departments)}
    )
    assert response.status_code == 200


def test_removing_a_department_applies_to_a_token_already_in_use(api):
    assert _work(api, SAM, "logistics").json()["outcome"] == "ok"

    _set_departments(api, "researcher")

    assert _work(api, SAM, "logistics").status_code == 403
    assert _work(api, SAM, "researcher").status_code == 200


def test_adding_a_department_applies_without_signing_in_again(api):
    assert _work(api, SAM, "hr").status_code == 403
    _set_departments(api, "logistics", "researcher", "hr")
    assert _work(api, SAM, "hr").status_code == 200


def test_pages_tied_to_a_department_follow_the_change(api):
    assert api.client.get("/v1/inbox", params={"department": "account"}, headers=SAM).status_code == 403
    _set_departments(api, "account")
    assert api.client.get("/v1/inbox", params={"department": "account"}, headers=SAM).status_code == 200
    _set_departments(api)
    assert api.client.get("/v1/inbox", params={"department": "account"}, headers=SAM).status_code == 403


def test_roles_written_into_a_token_are_ignored(api):
    claims_admin = _token("sam", "admin")
    assert api.client.get("/v1/approvals", headers=claims_admin).status_code == 403
    assert api.client.get("/v1/dashboard", headers=claims_admin).json()["roles"] == ["logistics", "researcher"]


def test_deleted_account_loses_access_immediately(api):
    assert api.client.get("/v1/dashboard", headers=SAM).status_code == 200
    with api.Session() as s:
        s.delete(s.get(User, "sam"))
        s.commit()
    response = api.client.get("/v1/dashboard", headers=SAM)
    assert response.status_code == 401 and "no longer exists" in response.json()["detail"]


def test_token_for_an_unknown_user_is_rejected(api):
    assert api.client.get("/v1/dashboard", headers=_token("ghost", "admin")).status_code == 401
