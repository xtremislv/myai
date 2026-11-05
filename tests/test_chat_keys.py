"""Per-user chat keys: the password flow over HTTP, key handling, and at-rest guards."""
from __future__ import annotations

import os
import typing
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidTag
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import (
    subject_from_claims as _REAL_SUBJECT_LOOKUP,  # conftest patches the attribute
)
from app.auth.security import hash_password, issue_token
from app.db.session import Base, User, get_session
from app.departments.registry import seed_departments
from app.llm import client as llm
from app.main import app
from app.orchestrator import execution
from app.orchestrator.graph import build_graph
from app.orchestrator.state import AgentState
from app.vault import user_keys

INITIAL = "initial-from-admin"
MINE = "my-own-long-password"
ADMIN = {"authorization": f"Bearer {issue_token('root', 'root', ['admin'])}"}


def _user():
    return SimpleNamespace(id="u1", chat_key_salt=None, chat_key_nonce=None, wrapped_chat_key=None)


# ---- keys ----
def test_the_right_password_unlocks_and_a_wrong_one_does_not():
    user = _user()
    key = user_keys.provision(user, MINE)
    assert key not in user.wrapped_chat_key
    assert user_keys.unlock(user, MINE) == key
    with pytest.raises(InvalidTag):
        user_keys.unlock(user, "wrong-password")


def test_wrapped_key_cannot_be_moved_to_another_user():
    user = _user()
    user_keys.provision(user, MINE)
    user.id = "u2"
    with pytest.raises(InvalidTag):
        user_keys.unlock(user, MINE)


def test_changing_password_keeps_the_key_and_retires_the_old_password():
    user = _user()
    key = user_keys.provision(user, MINE)
    assert user_keys.rewrap(user, MINE, "another-long-password") == key
    assert user_keys.unlock(user, "another-long-password") == key
    with pytest.raises(InvalidTag):
        user_keys.unlock(user, MINE)


def test_sealed_key_only_opens_for_its_subject():
    key = os.urandom(32)
    sealed = user_keys.seal_for_token(key, "alice")
    assert user_keys.open_from_token(sealed, "alice") == key
    with pytest.raises(InvalidTag):
        user_keys.open_from_token(sealed, "bob")


def test_serializer_encrypts_and_needs_the_same_key():
    key = os.urandom(32)
    typed = user_keys.checkpoint_serde(key).dumps_typed({"text": "SECRET"})
    assert b"SECRET" not in typed[1]
    assert user_keys.checkpoint_serde(key).loads_typed(typed) == {"text": "SECRET"}
    with pytest.raises(InvalidTag):
        user_keys.checkpoint_serde(os.urandom(32)).loads_typed(typed)


def test_state_has_no_free_text_string_fields():
    """PostgresSaver stores str/int/float/bool channels inline as plain JSON, skipping the
    serializer. Any free-text field must be a list or dict, or it is stored unencrypted."""
    hints = typing.get_type_hints(AgentState)
    # active_agent only ever holds a specialist name from specialists.SPECIALISTS.
    assert {name for name, hint in hints.items() if hint is str} == {"subject_id", "next", "active_agent"}


# ---- the flow over HTTP ----
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
        yield build_graph().compile(checkpointer=saver)

    monkeypatch.setattr(execution, "_graph_cm", _cm)
    prompts = []

    def _chat(messages, **kwargs):
        prompts.append(messages)
        return "ok"

    monkeypatch.setattr(llm, "chat", _chat)
    app.dependency_overrides[get_session] = _get_session
    client = TestClient(app)
    assert client.post(
        "/v1/auth/users", headers=ADMIN, json={"username": "alice", "password": INITIAL, "roles": ["hr"]}
    ).status_code == 201
    yield SimpleNamespace(client=client, prompts=prompts, Session=Session, saver=saver)
    app.dependency_overrides.clear()


def _login(api, password):
    return api.client.post("/v1/auth/login", json={"username": "alice", "password": password})


def _bearer(body):
    return {"authorization": f"Bearer {body['access_token']}"}


def _chat_as(api, headers, message="hello"):
    return api.client.post("/v1/chat", headers=headers, json={"message": message, "thread_id": "t"})


def _change(api, headers, old, new):
    return api.client.post(
        "/v1/auth/change-password", headers=headers, json={"old_password": old, "new_password": new}
    )


def _alice(api):
    with api.Session() as s:
        return s.query(User).filter(User.username == "alice").one()


def test_private_chat_is_refused_until_the_user_sets_their_own_password(api):
    first = _login(api, INITIAL).json()
    assert first["must_change_password"] is True
    refused = _chat_as(api, _bearer(first))
    assert refused.status_code == 403 and "password" in refused.json()["detail"]
    assert _alice(api).wrapped_chat_key is None  # nothing the admin's password could open

    changed = _change(api, _bearer(first), INITIAL, MINE)
    assert changed.status_code == 200 and changed.json()["must_change_password"] is False
    assert _chat_as(api, _bearer(changed.json())).status_code == 200


def test_key_is_not_derivable_from_the_admins_initial_password(api):
    first = _login(api, INITIAL).json()
    _change(api, _bearer(first), INITIAL, MINE)
    with pytest.raises(InvalidTag):
        user_keys.unlock(_alice(api), INITIAL)
    assert _login(api, INITIAL).status_code == 401


def test_chats_survive_a_later_password_change(api):
    first = _change(api, _bearer(_login(api, INITIAL).json()), INITIAL, MINE).json()
    _chat_as(api, _bearer(first), "REMEMBER THIS")

    _change(api, _bearer(first), MINE, "a-different-long-password")
    fresh = _login(api, "a-different-long-password").json()
    _chat_as(api, _bearer(fresh), "what did I say?")

    assert any("REMEMBER THIS" in m["content"] for m in api.prompts[-1])


def test_login_gives_a_token_that_opens_earlier_chats(api):
    first = _change(api, _bearer(_login(api, INITIAL).json()), INITIAL, MINE).json()
    _chat_as(api, _bearer(first), "REMEMBER THIS")

    _chat_as(api, _bearer(_login(api, MINE).json()), "what did I say?")

    assert any("REMEMBER THIS" in m["content"] for m in api.prompts[-1])


def test_change_password_rejects_bad_input(api):
    headers = _bearer(_login(api, INITIAL).json())
    assert _change(api, headers, "wrong-old-password", MINE).status_code == 401
    assert _change(api, headers, INITIAL, INITIAL).status_code == 400
    assert _change(api, headers, INITIAL, "short").status_code == 422


def test_token_without_a_key_or_with_someone_elses_key_cannot_chat(api):
    assert _chat_as(api, ADMIN).status_code == 403

    import jwt

    from app.config import get_settings

    settings = get_settings()
    claims = {"sub": "bob", "roles": [], "ck": user_keys.seal_for_token(b"k" * 32, "alice")}
    forged = jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    assert _chat_as(api, {"authorization": f"Bearer {forged}"}).status_code == 401


def test_password_above_bcrypts_limit_cannot_login_with_a_matching_prefix(api):
    """bcrypt ignores everything after 72 bytes; the key must still need the whole password."""
    long_password = "x" * 80
    with api.Session() as s:
        user = s.query(User).filter(User.username == "alice").one()
        user.password_hash = hash_password(long_password)
        user_keys.provision(user, long_password)
        s.commit()
    assert _login(api, long_password).status_code == 200
    assert _login(api, "x" * 72 + "y" * 8).status_code == 401


# ---- admin: password reset and disabling accounts ----
def _real_lookup(monkeypatch):
    """conftest lets tokens without a user row through; these tests need the real check."""
    from app.api import deps

    monkeypatch.setattr(deps, "subject_from_claims", _REAL_SUBJECT_LOOKUP)


def _own_password_and_a_chat(api):
    first = _bearer(_login(api, INITIAL).json())
    changed = _change(api, first, INITIAL, MINE).json()
    headers = _bearer(changed)
    assert _chat_as(api, headers).status_code == 200
    return headers, changed["recovery_code"]


def _threads(api, headers):
    response = api.client.get("/v1/chat/threads", headers=headers)
    assert response.status_code == 200, response.text
    return [t["thread_id"] for t in response.json()]


def test_reset_password_keeps_chats_for_someone_with_their_recovery_code(api):
    headers, code = _own_password_and_a_chat(api)
    assert _threads(api, headers) == ["t"]
    alice = _alice(api).id
    reset = api.client.post(f"/v1/auth/users/{alice}/reset-password", headers=ADMIN, json={"password": "temporary-from-admin"})
    assert reset.status_code == 200 and reset.json()["chat_enabled"] is False
    assert _login(api, MINE).status_code == 401
    recovered = api.client.post(
        "/v1/auth/recover", json={"username": "alice", "recovery_code": code, "new_password": "brand-new-password"}
    )
    assert recovered.status_code == 200
    assert _threads(api, _bearer(recovered.json())) == ["t"]


def test_reset_password_without_the_code_starts_fresh_and_never_gives_the_admin_the_key(api):
    headers, _ = _own_password_and_a_chat(api)
    alice = _alice(api).id
    api.client.post(f"/v1/auth/users/{alice}/reset-password", headers=ADMIN, json={"password": "temporary-from-admin"})
    temporary = _login(api, "temporary-from-admin").json()
    assert temporary["must_change_password"] is True  # the admin's password never unlocks a chat key
    fresh = _bearer(_change(api, _bearer(temporary), "temporary-from-admin", "brand-new-password").json())
    assert _threads(api, fresh) == []  # the old chat can't be opened, and listing doesn't break


def test_disabled_account_is_locked_out_at_once_and_can_be_re_enabled(api, monkeypatch):
    _real_lookup(monkeypatch)
    with api.Session() as s:  # the real lookup needs the admin to exist too
        s.add(User(id="root", username="root", password_hash=hash_password("x" * 14), roles=["admin"]))
        s.commit()
    headers, code = _own_password_and_a_chat(api)
    alice = _alice(api).id
    url = f"/v1/auth/users/{alice}/disabled"
    assert api.client.put(url, headers=ADMIN, json={"disabled": True}).json()["disabled"] is True
    assert _chat_as(api, headers).status_code == 403  # a live token stops working
    assert _login(api, MINE).status_code == 403
    assert api.client.post(
        "/v1/auth/recover", json={"username": "alice", "recovery_code": code, "new_password": "x" * 14}
    ).status_code == 403
    assert api.client.put(url, headers=ADMIN, json={"disabled": False}).status_code == 200
    assert _login(api, MINE).status_code == 200


def test_admin_cannot_disable_or_reset_their_own_account(api):
    for method, path, body in [
        ("put", "/v1/auth/users/root/disabled", {"disabled": True}),
        ("post", "/v1/auth/users/root/reset-password", {"password": "another-long-pass"}),
    ]:
        assert getattr(api.client, method)(path, headers=ADMIN, json=body).status_code == 400


def test_only_the_admin_can_manage_accounts(api):
    alice = _alice(api).id
    staff = {"authorization": f"Bearer {issue_token('bob', 'bob', ['hr'])}"}
    assert api.client.put(f"/v1/auth/users/{alice}/disabled", headers=staff, json={"disabled": True}).status_code == 403
    assert api.client.post(
        f"/v1/auth/users/{alice}/reset-password", headers=staff, json={"password": "another-long-pass"}
    ).status_code == 403


# ---- staying signed in across a reload (2026-11-05) ----
def test_the_session_cookie_renews_the_token_only_with_its_header_and_ends_on_sign_out(api):
    first = _change(api, _bearer(_login(api, INITIAL).json()), INITIAL, MINE)
    _chat_as(api, _bearer(first.json()), "REMEMBER THIS")
    login = _login(api, MINE)
    cookie = login.cookies.get("tw_session")
    assert cookie and "httponly" in login.headers["set-cookie"].lower()
    assert "path=/v1/auth/session" in login.headers["set-cookie"].lower()
    assert "samesite=strict" in login.headers["set-cookie"].lower()
    jar = {"tw_session": cookie}
    assert api.client.post("/v1/auth/session", cookies=jar).status_code == 401  # a form from elsewhere can't
    renewed = api.client.post("/v1/auth/session", cookies=jar, headers={"x-tw-session": "1"})
    assert renewed.status_code == 200 and renewed.json()["access_token"] != login.json()["access_token"]
    _chat_as(api, _bearer(renewed.json()), "what did I say?")  # the renewed token still opens private chats
    assert any("REMEMBER THIS" in m["content"] for m in api.prompts[-1])
    assert api.client.post("/v1/auth/session", headers={"x-tw-session": "1"},
                           cookies={"tw_session": "forged"}).status_code == 401
    out = api.client.delete("/v1/auth/session")
    assert out.status_code == 204 and "tw_session=" in out.headers["set-cookie"]
