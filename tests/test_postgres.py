"""Runs the real checkpointer and migrations against a real Postgres.

Skipped unless POSTGRES_INTEGRATION=1 and POSTGRES_DSN points at a throwaway database.
These cover what SQLite and MemorySaver cannot: how PostgresSaver lays out rows, which
decides whether private text is actually encrypted at rest.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from cryptography.exceptions import InvalidTag

from app.acl.models import Subject
from app.db.session import Base, engine, init_db
from app.llm import client as llm
from app.orchestrator import execution, tools
from app.orchestrator.execution import PendingAction

pytestmark = pytest.mark.skipif(
    os.environ.get("POSTGRES_INTEGRATION") != "1", reason="needs a throwaway Postgres"
)

KEY = b"k" * 32
OTHER_KEY = b"o" * 32


@pytest.fixture(scope="module", autouse=True)
def migrated():
    init_db()


def _dump_all_checkpoint_data() -> bytes:
    """Everything LangGraph persisted, as one searchable blob."""
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        out = b""
        cur.execute("SELECT checkpoint::text, metadata::text FROM checkpoints")
        for row in cur.fetchall():
            out += " ".join(row).encode()
        for table in ("checkpoint_blobs", "checkpoint_writes"):
            cur.execute(f"SELECT blob FROM {table}")  # noqa: S608
            out += b"".join(bytes(r[0]) for r in cur.fetchall() if r[0] is not None)
        return out
    finally:
        raw.close()


def _llm_by_agent(monkeypatch, marker):
    """Every reply repeats the marker, so model output is checked as well as input."""

    def _chat(messages, **kwargs):
        system = messages[0]["content"]
        if "RESEARCHER" in system:
            return json.dumps({"done": True})
        return f"reply about {marker}"

    monkeypatch.setattr(llm, "chat", _chat)


def test_migrations_match_the_models_on_postgres():
    def include_name(name, type_, parent_names):
        return type_ != "table" or name in Base.metadata.tables

    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"include_name": include_name})
        assert compare_metadata(context, Base.metadata) == []


@pytest.mark.parametrize("mode", ["single", "team"])
def test_private_text_is_not_readable_in_the_database(monkeypatch, mode):
    marker = f"PRIVATE-{uuid.uuid4().hex}"
    _llm_by_agent(monkeypatch, marker)
    subject = Subject(f"user-{uuid.uuid4().hex}", ("researcher",))

    execution.run(subject, f"my secret is {marker}", "t", mode, None, chat_key=KEY)

    assert marker.encode() not in _dump_all_checkpoint_data()


@pytest.mark.parametrize("mode", ["single", "team"])
def test_the_scan_finds_text_when_it_is_not_encrypted(monkeypatch, mode):
    """Proves the test above can fail."""
    marker = f"PUBLIC-{uuid.uuid4().hex}"
    _llm_by_agent(monkeypatch, marker)
    subject = Subject(f"user-{uuid.uuid4().hex}", ("researcher",))

    execution.run(subject, f"work note {marker}", "t", mode, "researcher")

    assert marker.encode() in _dump_all_checkpoint_data()


def test_private_chat_continues_with_the_right_key_and_fails_with_another(monkeypatch):
    marker = f"PRIVATE-{uuid.uuid4().hex}"
    prompts = []

    def _chat(messages, **kwargs):
        prompts.append(messages)
        return "ok"

    monkeypatch.setattr(llm, "chat", _chat)
    subject = Subject(f"user-{uuid.uuid4().hex}", ())

    execution.run(subject, marker, "t", "single", None, chat_key=KEY)
    execution.run(subject, "what did I say?", "t", "single", None, chat_key=KEY)
    assert any(marker in m["content"] for m in prompts[-1])

    with pytest.raises(InvalidTag):
        execution.run(subject, "what did I say?", "t", "single", None, chat_key=OTHER_KEY)


@pytest.mark.parametrize("approved", [True, False])
def test_critical_tool_pauses_and_resumes_from_a_real_checkpoint(monkeypatch, approved):
    ran = []
    monkeypatch.setitem(tools.TOOLS, "manage_bill", lambda state, bill_id: ran.append(bill_id) or "ok")
    replies = [json.dumps({"tool": "manage_bill", "args": {"bill_id": "B1"}}), "finished"]
    monkeypatch.setattr(llm, "chat", lambda messages, **k: replies.pop(0))
    subject = Subject(f"user-{uuid.uuid4().hex}", ("account",))

    pending = execution.run(subject, "record it", "t", "single", "account")

    assert isinstance(pending, PendingAction) and ran == []
    reply = execution.resume(
        pending.checkpoint_thread, "single", approved=approved, reason="",
        tool=pending.tool, args=pending.args,
    )
    assert reply == "finished"
    assert ran == (["B1"] if approved else [])


def test_resume_refuses_a_different_call_on_a_real_checkpoint(monkeypatch):
    ran = []
    monkeypatch.setitem(tools.TOOLS, "manage_bill", lambda state, bill_id: ran.append(bill_id) or "ok")
    monkeypatch.setattr(
        llm, "chat", lambda messages, **k: json.dumps({"tool": "manage_bill", "args": {"bill_id": "B1"}})
    )
    subject = Subject(f"user-{uuid.uuid4().hex}", ("account",))
    pending = execution.run(subject, "record it", "t", "single", "account")

    with pytest.raises(execution.StaleApproval):
        execution.resume(
            pending.checkpoint_thread, "single", approved=True, reason="",
            tool="manage_bill", args={"bill_id": "SOMETHING-ELSE"},
        )
    assert ran == []
