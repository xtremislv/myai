"""SQLAlchemy engine/session + ORM models for users, ACL, and vault."""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    LargeBinary,
    String,
    Text,
    create_engine,
    func,
)
from sqlalchemy import false as sa_false
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)

from app.config import get_settings

_settings = get_settings()
# SQLAlchemy wants the psycopg3 driver prefix.
_engine_dsn = _settings.postgres_dsn.replace("postgresql://", "postgresql+psycopg://")
engine = create_engine(_engine_dsn, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    roles: Mapped[list] = mapped_column(JSON, default=list)
    # A disabled account can't sign in, and its live tokens stop working on the next request.
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())
    # Private-chat key, wrapped by the user's password (app/vault/user_keys.py).
    # Empty until the user replaces the initial password the admin set.
    chat_key_salt: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    chat_key_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    wrapped_chat_key: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # The same key, wrapped by the user's one-time recovery code.
    recovery_salt: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    recovery_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    wrapped_recovery_key: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # Personal settings: timezone, email address, daily mail digest hour, email copies.
    prefs: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    last_digest_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ACLPolicy(Base):
    """A single allow/deny rule bound to a role (or '*' subject)."""

    __tablename__ = "acl_policies"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject: Mapped[str] = mapped_column(String(128), index=True)  # role name, user id, or "*"
    effect: Mapped[str] = mapped_column(String(8))  # "allow" | "deny"
    action: Mapped[str] = mapped_column(String(32))  # read|write|delete|list|admin|*
    resource: Mapped[str] = mapped_column(String(256))  # e.g. "object:reports/*"
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VaultSecret(Base):
    """Envelope-encrypted secret. Master key never stored here."""

    __tablename__ = "vault_secrets"
    name: Mapped[str] = mapped_column(String(256), primary_key=True)
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary)  # data key wrapped by master key
    nonce: Mapped[bytes] = mapped_column(LargeBinary)        # AES-GCM nonce for payload
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)   # AES-GCM(payload)
    key_nonce: Mapped[bytes] = mapped_column(LargeBinary)    # AES-GCM nonce for wrapping
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Department(Base):
    """Department (= an ACL role) with its routing/quota config.

    The ACL *grants* for a department live in `acl_policies` (subject = the
    department name); this row holds the metadata the Request Manager needs:
    which tools the department may use, its quota/concurrency, whether its
    critical actions require approval, and which resources count as critical.
    """

    __tablename__ = "departments"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(String(512), default="")
    grants: Mapped[list] = mapped_column(JSON, default=list)            # [{resource, actions:[...]}]
    tools: Mapped[list] = mapped_column(JSON, default=list)             # tool names ("*" = all)
    quota_per_min: Mapped[int] = mapped_column(default=60)
    max_concurrency: Mapped[int] = mapped_column(default=4)
    requires_approval: Mapped[bool] = mapped_column(default=False)
    critical_resources: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Approval(Base):
    """A critical tool call paused in the graph, waiting on a human decision."""

    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    department: Mapped[str] = mapped_column(String(64), index=True)
    subject_id: Mapped[str] = mapped_column(String(128), index=True)
    message: Mapped[str] = mapped_column(Text)
    thread_id: Mapped[str] = mapped_column(String(128))
    mode: Mapped[str] = mapped_column(String(16), default="single")
    tool: Mapped[str] = mapped_column(String(64))
    tool_args: Mapped[dict] = mapped_column(JSON, default=dict)
    checkpoint_thread: Mapped[str] = mapped_column(String(256), index=True)  # where the run is paused
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # inbox task, if any
    chat_thread: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)  # shown there
    result: Mapped[str | None] = mapped_column(Text, nullable=True)  # the agent's reply once decided
    state: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|approved|denied|failed
    reason: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)


class Report(Base):
    """A flagged request (e.g. NSFW) escalated to one or more departments."""

    __tablename__ = "reports"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # e.g. "nsfw"
    subject_id: Mapped[str] = mapped_column(String(128), index=True)
    message: Mapped[str] = mapped_column(Text)
    notified: Mapped[list] = mapped_column(JSON, default=list)  # departments notified
    state: Mapped[str] = mapped_column(String(16), default="open")  # open|reviewed
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DepartmentTask(Base):
    """A request forwarded to a department's inbox (e.g. order/bill to account)."""

    __tablename__ = "department_tasks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    department: Mapped[str] = mapped_column(String(64), index=True)
    requester_id: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="request")  # order|bill|service|request
    message: Mapped[str] = mapped_column(Text)
    thread_id: Mapped[str] = mapped_column(String(128))
    mode: Mapped[str] = mapped_column(String(16), default="single")
    state: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued|processing|awaiting|done|denied|failed
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    chat_thread: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)  # shown there
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatHistory(Base):
    """Attachments on a chat, deleted after 3 days by scripts/purge_history.py.

    Message text is not stored here: it stays encrypted in the checkpoints, kept
    indefinitely. `artifacts` is a list of {"ref", "meta"}: ref is the object key in
    MinIO, meta is the file's name, type and size, encrypted with the owner's key.
    """

    __tablename__ = "chat_history"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    thread_id: Mapped[str] = mapped_column(String(128), index=True)
    artifacts: Mapped[list] = mapped_column(JSON, default=list)  # media/file refs; purged after 3d
    artifacts_purged: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatThread(Base):
    """Lets a user list their private chats. The title is encrypted with their key."""

    __tablename__ = "chat_threads"
    owner_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    encrypted_title: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserFile(Base):
    """A file in a person's own "My files" area.

    The path is encrypted with the owner's chat key, and so is every stored version of the
    content (MinIO key userfiles/<owner>/<id>/<version>), so only the owner can open them.
    `versions` lists the kept versions, newest last: [{"v", "size", "at"}]. A deleted file
    stays in the trash (deleted_at) for a while so it can be restored.
    """

    __tablename__ = "user_files"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    encrypted_path: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    size: Mapped[int] = mapped_column(default=0)
    version: Mapped[int] = mapped_column(default=1)
    versions: Mapped[list] = mapped_column(JSON, default=list)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Reminder(Base):
    """A reminder for one person. The text is encrypted with the server's notes key
    (app/vault/notes.py): it must be readable when it falls due, without the person's key."""

    __tablename__ = "reminders"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    encrypted_text: Mapped[str] = mapped_column(Text)
    due_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    repeat: Mapped[str] = mapped_column(String(16), default="none")  # none|daily|weekdays|weekly|monthly
    email: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())
    source: Mapped[str] = mapped_column(String(16), default="chat")  # chat|mail|app
    # For reminders made from an email: a hash of its Message-ID, so a digest never adds it twice.
    source_ref: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|sent|cancelled
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Notification(Base):
    """Something to show a person under the bell: a due reminder, a mail digest, an alert.
    Title and body are encrypted with the server's notes key."""

    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # reminder|digest|alert|mail
    encrypted_title: Mapped[str] = mapped_column(Text)
    encrypted_body: Mapped[str] = mapped_column(Text, default="")
    read_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class OpsAlert(Base):
    """A Prometheus alert as last reported, so each one notifies the admin once when it
    starts and once when it resolves, however often Prometheus resends it."""

    __tablename__ = "ops_alerts"
    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    severity: Mapped[str] = mapped_column(String(32), default="")
    summary: Mapped[str] = mapped_column(String(512), default="")
    state: Mapped[str] = mapped_column(String(16), default="firing")  # firing|resolved
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    subject: Mapped[str] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(32))
    resource: Mapped[str] = mapped_column(String(256))
    decision: Mapped[str] = mapped_column(String(16))  # allow|deny|read|write
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with SessionLocal() as session:
        yield session


def init_db() -> None:
    """Bring the database schema up to the latest migration."""
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(config, "head")
    _setup_checkpoint_tables()


def _setup_checkpoint_tables() -> None:
    """LangGraph's own tables. This must run at startup and never during a request: its
    CREATE INDEX CONCURRENTLY waits for every open transaction, including the caller's."""
    if engine.dialect.name != "postgresql":
        return
    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(_settings.postgres_dsn) as checkpointer:
        checkpointer.setup()


def release_connection(session: Session) -> None:
    """End the session's transaction before a long model call, so that call does not hold
    a pooled connection and an open transaction for its whole duration."""
    session.commit()
