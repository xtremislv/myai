"""Recovery codes, attachments, chat history and the dashboard API, over HTTP."""
from __future__ import annotations

import datetime as dt
import io
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from pypdf import PdfWriter
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes_chat import safe_filename
from app.auth.security import issue_token
from app.db.session import Base, ChatHistory, ChatThread, Report, User, get_session
from app.departments.registry import seed_departments
from app.llm import client as llm
from app.main import app
from app.orchestrator import execution
from app.orchestrator.graph import build_graph
from app.orchestrator.multi_graph import build_team_graph
from app.request_manager import guards
from app.request_manager.retention import purge_expired
from app.storage import attachments

ADMIN = {"authorization": f"Bearer {issue_token('root', 'root', ['admin'])}"}
MINE = "my-own-long-password"


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
        builder = build_team_graph() if mode == "team" else build_graph()
        yield builder.compile(checkpointer=saver)

    monkeypatch.setattr(execution, "_graph_cm", _cm)

    objects: dict[str, bytes] = {}
    monkeypatch.setattr(attachments, "_put", objects.__setitem__)
    monkeypatch.setattr(attachments, "_get", objects.__getitem__)
    monkeypatch.setattr(attachments, "_remove", objects.pop)

    prompts = []

    def _chat(messages, **kwargs):
        prompts.append(messages)
        return "ok"

    monkeypatch.setattr(llm, "chat", _chat)
    app.dependency_overrides[get_session] = _get_session
    client = TestClient(app)
    yield SimpleNamespace(client=client, Session=Session, objects=objects, prompts=prompts, saver=saver)
    app.dependency_overrides.clear()


def _bearer(body):
    return {"authorization": f"Bearer {body['access_token']}"}


def _user(api, name="alice", roles=("hr",)):
    """Create a user who has set their own password. Returns their token response."""
    initial = f"initial-{name}-pw"
    assert api.client.post(
        "/v1/auth/users", headers=ADMIN, json={"username": name, "password": initial, "roles": list(roles)}
    ).status_code == 201
    first = api.client.post("/v1/auth/login", json={"username": name, "password": initial}).json()
    return api.client.post(
        "/v1/auth/change-password", headers=_bearer(first),
        json={"old_password": initial, "new_password": MINE},
    ).json()


def _chat(api, headers, message, thread="t"):
    return api.client.post("/v1/chat", headers=headers, json={"message": message, "thread_id": thread})


def _upload(api, headers, name="notes.txt", data=b"QUARTERLY FIGURES", thread="t", content_type="text/plain"):
    return api.client.post(
        "/v1/chat/attachments", headers=headers, data={"thread_id": thread},
        files={"file": (name, data, content_type)},
    )


def _recover(api, code, password="a-new-long-password", username="alice"):
    return api.client.post(
        "/v1/auth/recover", json={"username": username, "recovery_code": code, "new_password": password}
    )


# ---- recovery codes ----
def test_setting_a_password_issues_a_code_once(api):
    first = _user(api)
    assert first["recovery_code"]
    login = api.client.post("/v1/auth/login", json={"username": "alice", "password": MINE}).json()
    assert login["recovery_code"] is None
    with api.Session() as s:
        user = s.query(User).filter(User.username == "alice").one()
    assert first["recovery_code"].encode() not in user.wrapped_recovery_key


def test_recovery_sets_a_new_password_and_keeps_the_chats(api):
    tokens = _user(api)
    _chat(api, _bearer(tokens), "REMEMBER THIS")

    recovered = _recover(api, tokens["recovery_code"])

    assert recovered.status_code == 200
    assert api.client.post("/v1/auth/login", json={"username": "alice", "password": MINE}).status_code == 401
    login = api.client.post(
        "/v1/auth/login", json={"username": "alice", "password": "a-new-long-password"}
    ).json()
    _chat(api, _bearer(login), "what did I say?")
    assert any("REMEMBER THIS" in m["content"] for m in api.prompts[-1])


def test_a_recovery_code_works_only_once_and_a_new_one_is_issued(api):
    code = _user(api)["recovery_code"]
    new_code = _recover(api, code).json()["recovery_code"]
    assert new_code and new_code != code
    assert _recover(api, code, "yet-another-long-password").status_code == 401
    assert _recover(api, new_code, "yet-another-long-password").status_code == 200


def test_code_is_accepted_however_it_is_typed(api):
    code = _user(api)["recovery_code"]
    assert _recover(api, " " + code.replace("-", " ").lower() + " ").status_code == 200


@pytest.mark.parametrize("username", ["alice", "nobody"])
def test_bad_recovery_attempts_look_identical(api, username):
    _user(api)
    bad = _recover(api, "AAAA-BBBB", username=username)
    assert bad.status_code == 401 and bad.json()["detail"] == "bad username or recovery code"


def test_one_users_code_does_not_work_for_another(api):
    alice = _user(api)
    _user(api, "bob")
    assert _recover(api, alice["recovery_code"], username="bob").status_code == 401


def test_user_who_never_set_a_password_has_nothing_to_recover(api):
    api.client.post("/v1/auth/users", headers=ADMIN, json={"username": "new", "password": "x" * 12})
    assert _recover(api, "AAAA", username="new").status_code == 401


def test_password_change_keeps_the_code_valid(api):
    tokens = _user(api)
    api.client.post(
        "/v1/auth/change-password", headers=_bearer(tokens),
        json={"old_password": MINE, "new_password": "changed-long-password"},
    )
    assert _recover(api, tokens["recovery_code"]).status_code == 200


def test_replacing_a_lost_code_retires_the_old_one(api):
    tokens = _user(api)
    url = "/v1/auth/recovery-code"
    assert api.client.post(url, headers=_bearer(tokens), json={"password": "wrong"}).status_code == 401

    new_code = api.client.post(url, headers=_bearer(tokens), json={"password": MINE}).json()["recovery_code"]

    assert _recover(api, tokens["recovery_code"]).status_code == 401
    assert _recover(api, new_code).status_code == 200


# ---- attachments ----
def test_upload_list_download_delete(api):
    headers = _bearer(_user(api))

    saved = _upload(api, headers).json()
    assert saved["outcome"] == "ok"
    attachment = saved["attachment"]
    assert (attachment["name"], attachment["size"]) == ("notes.txt", 17)

    listed = api.client.get("/v1/chat/attachments", headers=headers, params={"thread_id": "t"}).json()
    assert [a["id"] for a in listed] == [attachment["id"]]
    assert api.client.get("/v1/chat/attachments", headers=headers, params={"thread_id": "x"}).json() == []

    got = api.client.get(f"/v1/chat/attachments/{attachment['id']}", headers=headers)
    assert got.content == b"QUARTERLY FIGURES"
    assert got.headers["content-type"] == "application/octet-stream"
    assert got.headers["content-disposition"].startswith("attachment;")

    assert api.client.delete(f"/v1/chat/attachments/{attachment['id']}", headers=headers).status_code == 204
    assert api.objects == {}
    assert api.client.get(f"/v1/chat/attachments/{attachment['id']}", headers=headers).status_code == 404


def test_file_and_its_name_are_encrypted_at_rest(api):
    headers = _bearer(_user(api))
    attachment = _upload(api, headers, name="medical-records.txt").json()["attachment"]

    assert all(b"QUARTERLY" not in blob for blob in api.objects.values())
    with api.Session() as s:
        row = s.get(ChatHistory, attachment["id"])
    assert "medical-records" not in str(row.artifacts)


def test_nobody_else_can_reach_an_attachment(api):
    alice = _bearer(_user(api))
    bob = _bearer(_user(api, "bob"))
    admin = _bearer(_user(api, "second-admin", ("admin",)))
    attachment_id = _upload(api, alice).json()["attachment"]["id"]

    for other in (bob, admin):
        assert api.client.get(f"/v1/chat/attachments/{attachment_id}", headers=other).status_code == 404
        assert api.client.delete(f"/v1/chat/attachments/{attachment_id}", headers=other).status_code == 404
        assert api.client.get("/v1/chat/attachments", headers=other).json() == []


def test_attachments_need_a_chat_key(api):
    api.client.post("/v1/auth/users", headers=ADMIN, json={"username": "new", "password": "x" * 12})
    first = api.client.post("/v1/auth/login", json={"username": "new", "password": "x" * 12}).json()
    assert _upload(api, _bearer(first)).status_code == 403


def test_size_limit_and_empty_files(api, monkeypatch):
    headers = _bearer(_user(api))
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "attachment_max_bytes", 5)
    assert _upload(api, headers, data=b"123456").status_code == 413
    assert _upload(api, headers, data=b"12345").status_code == 200
    assert _upload(api, headers, data=b"").status_code == 400


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("../../etc/pass\x01wd.txt", "passwd.txt"),
        ("c:\\users\\me\\report.txt", "report.txt"),
        ("line\r\nbreak.txt", "linebreak.txt"),
        ("", "file"),
        (None, "file"),
        ("x" * 300, "x" * 200),
    ],
)
def test_unsafe_filenames_are_reduced_to_a_plain_name(given, expected):
    assert safe_filename(given) == expected


def test_uploaded_path_is_stored_as_a_plain_name(api):
    saved = _upload(api, _bearer(_user(api)), name="../../etc/passwd.txt").json()["attachment"]
    assert saved["name"] == "passwd.txt"


def test_model_reads_attachment_text_but_it_is_never_checkpointed(api):
    headers = _bearer(_user(api))
    _upload(api, headers, data=b"THE ATTACHED FIGURES")

    _chat(api, headers, "summarize my file")

    assert "THE ATTACHED FIGURES" in api.prompts[-1][0]["content"]
    stored = api.saver.get_tuple({"configurable": {"thread_id": "x"}})
    assert stored is None
    for thread in api.saver.storage.values():
        for namespace in thread.values():
            for checkpoint in namespace.values():
                typed, _, _ = checkpoint
                assert b"THE ATTACHED FIGURES" not in typed[1]
    history = api.client.get("/v1/chat/threads/t/messages", headers=headers).json()
    assert not any("THE ATTACHED FIGURES" in m["content"] for m in history)


def test_team_mode_also_receives_attachment_text(api):
    headers = _bearer(_user(api))
    _upload(api, headers, data=b"THE ATTACHED FIGURES")
    api.client.post("/v1/chat", headers=headers, json={"message": "go", "thread_id": "t", "mode": "team"})
    # planner and writer; with no tools in a private chat the researcher makes no call
    assert len(api.prompts) >= 2
    assert all("THE ATTACHED FIGURES" in p[0]["content"] for p in api.prompts)


def test_attachment_from_another_thread_is_not_read(api):
    headers = _bearer(_user(api))
    _upload(api, headers, data=b"THE ATTACHED FIGURES", thread="other")
    _chat(api, headers, "hello", thread="t")
    assert "THE ATTACHED FIGURES" not in api.prompts[-1][0]["content"]


def test_pdf_text_is_read_and_a_broken_pdf_is_stored_but_not_read(api):
    headers = _bearer(_user(api))
    buffer = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(buffer)
    assert attachments.extract_text("a.pdf", "application/pdf", buffer.getvalue()) is None  # no text

    saved = _upload(api, headers, name="broken.pdf", data=b"not a pdf", content_type="application/pdf")
    assert saved.json()["outcome"] == "ok"
    _chat(api, headers, "read it")
    assert "broken.pdf: this file type cannot be read" in api.prompts[-1][0]["content"]


def test_binary_file_is_stored_and_the_model_is_told_it_cannot_read_it(api):
    headers = _bearer(_user(api))
    _upload(api, headers, name="photo.png", data=b"\x89PNG", content_type="image/png")
    _chat(api, headers, "what is in my photo?")
    assert "photo.png: this file type cannot be read" in api.prompts[-1][0]["content"]


def test_attachment_text_is_capped(api, monkeypatch):
    headers = _bearer(_user(api))
    monkeypatch.setattr(attachments, "MAX_CONTEXT_CHARS", 10)
    _upload(api, headers, name="a.txt", data=b"x" * 50)
    _upload(api, headers, name="b.txt", data=b"y" * 50)
    _chat(api, headers, "read them")
    system = api.prompts[-1][0]["content"]
    assert "x" * 10 + "\n[truncated]" in system
    assert "b.txt: not included" in system


def test_unavailable_file_does_not_block_the_chat(api, monkeypatch):
    headers = _bearer(_user(api))
    _upload(api, headers)

    def _down(key):
        raise ConnectionError("minio is down")

    monkeypatch.setattr(attachments, "_get", _down)
    assert _chat(api, headers, "hello").status_code == 200
    assert "temporarily unavailable" in api.prompts[-1][0]["content"]


def test_flagged_file_is_rejected_and_reported(api, monkeypatch):
    headers = _bearer(_user(api))
    monkeypatch.setattr(guards, "model_verdict", lambda m: "illegal" if "BAD" in m else "none")

    res = _upload(api, headers, name="plan.txt", data=b"BAD CONTENT").json()

    assert res["outcome"] == "flagged_nsfw" and res["attachment"] is None
    assert api.objects == {}
    with api.Session() as s:
        [report] = s.query(Report).all()
    assert "plan.txt" in report.message and "BAD CONTENT" in report.message


# ---- 3-day retention, through the real purge ----
def _age(api, attachment_id, days):
    with api.Session() as s:
        row = s.get(ChatHistory, attachment_id)
        row.created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        s.commit()


def test_expired_file_disappears_even_before_the_purge_runs(api):
    headers = _bearer(_user(api))
    attachment_id = _upload(api, headers, data=b"OLD FILE").json()["attachment"]["id"]
    _age(api, attachment_id, 4)

    assert api.client.get(f"/v1/chat/attachments/{attachment_id}", headers=headers).status_code == 404
    assert api.client.get("/v1/chat/attachments", headers=headers).json() == []
    _chat(api, headers, "read it")
    assert "OLD FILE" not in api.prompts[-1][0]["content"]


def test_purge_removes_the_stored_file_and_the_text_stays(api):
    headers = _bearer(_user(api))
    attachment_id = _upload(api, headers, data=b"OLD FILE").json()["attachment"]["id"]
    _chat(api, headers, "KEEP THIS TEXT")
    _age(api, attachment_id, 4)

    with api.Session() as s:
        assert purge_expired(s, delete_object=api.objects.pop) == 1

    assert api.objects == {}
    history = api.client.get("/v1/chat/threads/t/messages", headers=headers).json()
    assert any(m["content"] == "KEEP THIS TEXT" for m in history)


# ---- chat history ----
def test_threads_and_messages_belong_to_their_owner(api):
    alice = _bearer(_user(api))
    bob = _bearer(_user(api, "bob"))
    _chat(api, alice, "PRIVATE TITLE AND A LONG FIRST MESSAGE " * 5, thread="mine")

    [thread] = api.client.get("/v1/chat/threads", headers=alice).json()
    assert thread["thread_id"] == "mine" and thread["title"].startswith("PRIVATE TITLE")
    assert len(thread["title"]) == 60
    messages = api.client.get("/v1/chat/threads/mine/messages", headers=alice).json()
    assert [m["role"] for m in messages] == ["user", "assistant"]

    assert api.client.get("/v1/chat/threads", headers=bob).json() == []
    assert api.client.get("/v1/chat/threads/mine/messages", headers=bob).json() == []


def test_thread_title_is_encrypted_at_rest(api):
    headers = _bearer(_user(api))
    _chat(api, headers, "SENSITIVE TITLE")
    with api.Session() as s:
        [row] = s.query(ChatThread).all()
    assert "SENSITIVE" not in row.encrypted_title


def test_history_hides_internal_team_messages(api):
    headers = _bearer(_user(api))
    api.client.post("/v1/chat", headers=headers, json={"message": "go", "thread_id": "t", "mode": "team"})
    messages = api.client.get("/v1/chat/threads/t/messages", headers=headers).json()
    assert (messages[0]["role"], messages[0]["content"]) == ("user", "go")
    assert not any(m["content"].startswith("[plan]") for m in messages)


def test_flagged_first_message_creates_no_thread(api, monkeypatch):
    headers = _bearer(_user(api))
    monkeypatch.setattr(guards, "model_verdict", lambda m: "explicit")
    _chat(api, headers, "something explicit")
    assert api.client.get("/v1/chat/threads", headers=headers).json() == []


# ---- dashboard API ----
def _token(*roles, user="someone"):
    return {"authorization": f"Bearer {issue_token(user, user, list(roles))}"}


def test_each_role_sees_only_its_own_counts(api):
    admin = api.client.get("/v1/dashboard", headers=ADMIN).json()
    assert {"pending_approvals", "users", "departments", "inbox_queued", "open_reports"} <= set(admin)

    accounts = api.client.get("/v1/dashboard", headers=_token("account")).json()
    assert "inbox_queued" in accounts and "open_reports" in accounts
    assert "pending_approvals" not in accounts and "users" not in accounts

    logistics = api.client.get("/v1/dashboard", headers=_token("logistics")).json()
    assert not {"inbox_queued", "open_reports", "pending_approvals", "departments"} & set(logistics)


def test_dashboard_requires_login(api):
    assert api.client.get("/v1/dashboard").status_code in (401, 403)


def test_private_chat_never_shows_up_in_dashboard_activity(api):
    headers = _bearer(_user(api))
    _chat(api, headers, "hello")
    assert api.client.get("/v1/dashboard", headers=ADMIN).json()["activity"] == []


def test_activity_is_everyones_for_admin_and_own_for_others(api):
    lee = _token("logistics", user="lee")
    api.client.post("/v1/request", headers=lee, json={"message": "track shipment", "thread_id": "t"})

    assert api.client.get("/v1/dashboard", headers=ADMIN).json()["activity"] == [
        {"department": "logistics", "outcome": "ok", "count": 1}
    ]
    assert api.client.get("/v1/dashboard", headers=lee).json()["activity"][0]["count"] == 1
    assert api.client.get("/v1/dashboard", headers=_token("logistics", user="other")).json()["activity"] == []


def test_user_list_is_admin_only_and_shows_no_key_material(api):
    _user(api)
    listed = api.client.get("/v1/auth/users", headers=ADMIN).json()
    assert listed[0]["chat_enabled"] is True and set(listed[0]) == {"id", "username", "roles", "chat_enabled", "disabled"}
    assert api.client.get("/v1/auth/users", headers=_token("hr")).status_code == 403


def test_people_are_shown_by_name_not_internal_id(api, monkeypatch):
    tokens = _user(api, "alice", ("logistics",))
    headers = _bearer(tokens)
    monkeypatch.setattr(guards, "model_verdict", lambda m: "explicit" if "BAD" in m else "none")
    _chat(api, headers, "BAD MESSAGE")
    api.client.post("/v1/request", headers=headers, json={"message": "pay the invoice", "thread_id": "t"})

    [report] = api.client.get("/v1/reports", headers=ADMIN).json()
    assert report["subject_name"] == "alice"
    [task] = api.client.get("/v1/inbox", params={"department": "account"}, headers=ADMIN).json()
    assert task["requester_name"] == "alice"


# ---- UI serving ----
def test_ui_is_served_with_a_strict_policy(api):
    assert api.client.get("/", follow_redirects=False).headers["location"] == "/ui/"
    page = api.client.get("/ui/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    policy = page.headers["content-security-policy"]
    assert "script-src 'self'" in policy and "unsafe-inline" not in policy
    assert api.client.get("/ui/app.js").status_code == 200
    assert api.client.get("/ui/style.css").status_code == 200


def test_ui_has_no_inline_code_or_third_party_assets(api):
    page = api.client.get("/ui/").text
    assert "<style" not in page and "style=" not in page and "onclick=" not in page
    assert "<script>" not in page and "http://" not in page and "https://" not in page
