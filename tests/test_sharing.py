"""Sharing with another person on the platform (2026-11-05: "send the findings and list to sayan" got
"what's Sayan's email?"). Accounts members may send a notification only, with nothing crucial in it."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import sharing
from app.auth.security import hash_password, issue_token
from app.db.session import Base, Notification, User, get_session
from app.departments.registry import seed_departments
from app.main import app
from app.orchestrator import nodes, tools
from app.reminders import service

KEY = os.urandom(32)


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        seed_departments(s)
        for uid, roles in (("lovy", ["researcher"]), ("sayan", ["researcher", "superuser"]),
                           ("asha", ["account"]), ("hari", ["hr"])):
            s.add(User(id=uid, username=uid.capitalize() if uid == "sayan" else uid,
                       password_hash=hash_password("a-long-password-1"), roles=roles, prefs={}))
        s.commit()
    monkeypatch.setattr(tools, "SessionLocal", Session)
    monkeypatch.setattr(nodes, "SessionLocal", Session)
    store: dict[str, bytes] = {}
    monkeypatch.setattr(sharing.objects, "put_system_object", lambda k, d: store.__setitem__(k, d))
    monkeypatch.setattr(sharing.objects, "get_system_object", lambda k: store[k])
    monkeypatch.setattr(sharing.objects, "remove_expired_object", lambda k: store.pop(k, None))
    monkeypatch.setattr(sharing.objects, "list_system_objects",
                        lambda p: [(k, len(v), None) for k, v in sorted(store.items()) if k.startswith(p)])
    return Session, store


def _state(uid, roles, dept=None):
    pad = {"mode": "single", **({"department": dept} if dept else {})}
    return {"subject_id": uid, "subject_roles": roles, "scratchpad": pad, "messages": []}


def _notes(Session, owner):
    with Session() as s:
        return service.notifications(s, owner)


def test_findings_and_a_file_reach_a_colleague_by_name_encrypted(db, monkeypatch):
    Session, store = db
    monkeypatch.setattr(tools, "_input_bytes", lambda state, path: ("apps.xlsx", b"SHEET-BYTES"))
    out = tools.share_with(_state("lovy", ["researcher"]), "sayan", note="Top apps: Bloom, District", files="Research/apps.xlsx")
    assert out.startswith("Shared with Sayan") and "apps.xlsx" in out
    assert not any(b"Bloom" in v or b"SHEET-BYTES" in v for v in store.values())  # stored encrypted
    [n] = _notes(Session, "sayan")
    assert n["title"] == "lovy shared 1 file with you" and "Bloom" in n["body"] and "Shared with me" in n["body"]
    listed = tools.shared_with_me(_state("sayan", ["researcher"]))
    assert "from lovy" in listed and "apps.xlsx" in listed
    with Session() as s:
        name, data = sharing.file_of(s, "sayan", sharing.received(s, "sayan")[0]["id"], 0)
    assert (name, data) == ("apps.xlsx", b"SHEET-BYTES")
    assert tools.shared_with_me(_state("hari", ["hr"])) == "Nothing has been shared with you."  # only the recipient


def test_names_are_found_loosely_and_unknown_names_list_the_people(db):
    Session, _ = db
    with Session() as s:
        assert sharing.find_person(s, "@SAYAN").id == "sayan"
        assert sharing.find_person(s, "say").id == "sayan"
        with pytest.raises(sharing.ShareError, match="no one called 'zed'.*Sayan"):
            sharing.find_person(s, "zed")


def test_at_work_only_members_of_the_department_receive(db):
    out = tools.share_with(_state("lovy", ["researcher"], dept="researcher"), "hari", note="notes")
    assert out.startswith("ERROR") and "isn't in researcher" in out
    assert tools.share_with(_state("lovy", ["researcher"], dept="researcher"), "sayan", note="notes").startswith("Shared")


def test_accounts_can_send_a_notice_only_never_files_or_crucial_details(db, monkeypatch):
    Session, _ = db
    monkeypatch.setattr(tools, "_input_bytes", lambda state, path: ("ledger.xlsx", b"x"))
    asha = _state("asha", ["account"])
    assert "notification only, never files" in tools.share_with(asha, "sayan", note="see file", files="ledger.xlsx")
    for crucial in ("Paid ₹45,000 to the vendor", "A/c 123456789012, IFSC HDFC0001234", "UPI ravi@okaxis",
                    "the account balance is low", "PAN ABCDE1234F"):
        assert tools.share_with(asha, "sayan", note=crucial).startswith("ERROR: not sent"), crucial
    monkeypatch.setattr(sharing, "crucial", lambda note, chat=None: "the Q3 margin figures")
    assert "Q3 margin" in tools.share_with(asha, "sayan", note="Our margins dropped a lot this quarter")
    monkeypatch.setattr(sharing, "crucial", lambda note, chat=None: None)
    assert tools.share_with(asha, "sayan", note="The invoice you asked about is done, please check with me.").startswith("Shared")
    assert not _notes(Session, "sayan")[0]["body"].count("₹")


def test_the_crucial_check_fails_closed():
    def broken(*a, **k):
        raise TimeoutError

    assert sharing.crucial("anything", chat=broken) == "the content couldn't be checked right now"
    assert sharing.crucial("All good", chat=lambda *a, **k: '{"crucial": false}') is None


def test_secrets_are_never_shared_by_anyone(db):
    lovy = _state("lovy", ["researcher"])
    assert "password" in tools.share_with(lovy, "sayan", note="wifi password: hunter22")
    assert "card number" in tools.share_with(lovy, "sayan", note="use 4111 1111 1111 1111")
    assert tools.share_with(lovy, "sayan", note="Bloom costs ₹199 and has 40k stars").startswith("Shared")  # fine for research


def test_private_chat_asks_the_person_to_confirm_and_work_does_not(db, monkeypatch):
    seen = []
    monkeypatch.setattr(nodes, "interrupt", lambda payload: seen.append(payload["tool"]) or {"approved": True})
    nodes.run_tool(_state("lovy", ["researcher"]), "share_with", {"to": "sayan", "note": "hi"})
    assert seen == ["share_with"]
    nodes.run_tool(_state("lovy", ["researcher"], dept="researcher"), "share_with", {"to": "sayan", "note": "hi"})
    assert seen == ["share_with"]  # at work: no approval request
    refused = nodes.run_tool(_state("asha", ["account"]), "share_with", {"to": "sayan", "note": "x", "files": "a.xlsx"})
    assert refused.startswith("ERROR") and seen == ["share_with"]  # refused before any confirmation card


def test_the_recipient_can_download_save_and_remove_it(db, monkeypatch):
    Session, _ = db

    def _get_session():
        with Session() as s:
            yield s

    monkeypatch.setattr(tools, "_input_bytes", lambda state, path: ("apps.csv", b"name\nBloom\n"))
    tools.share_with(_state("lovy", ["researcher"]), "sayan", note="list", files="apps.csv")
    app.dependency_overrides[get_session] = _get_session
    try:
        client = TestClient(app)
        h = {"authorization": "Bearer " + issue_token("sayan", "Sayan", ["researcher", "superuser"], chat_key=KEY)}
        [share] = client.get("/v1/shared", headers=h).json()
        assert share["sender"] == "lovy" and share["files"][0]["name"] == "apps.csv"
        assert client.get(f"/v1/shared/{share['id']}/files/0", headers=h).content == b"name\nBloom\n"
        saved = client.post(f"/v1/shared/{share['id']}/files/0/save", headers=h)
        assert saved.status_code == 200 and saved.json()["path"] == "Shared/apps.csv"
        other = {"authorization": "Bearer " + issue_token("hari", "hari", ["hr"], chat_key=KEY)}
        assert client.get(f"/v1/shared/{share['id']}/files/0", headers=other).status_code == 404
        assert client.delete(f"/v1/shared/{share['id']}", headers=h).status_code == 204
        assert client.get("/v1/shared", headers=h).json() == []
    finally:
        app.dependency_overrides.clear()
    with Session() as s:
        assert s.query(Notification).filter(Notification.owner_id == "sayan").count() == 1
