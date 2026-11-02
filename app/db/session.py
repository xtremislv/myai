"""SQLAlchemy engine/session + ORM models for users, ACL, and vault."""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    JSON,
    DateTime,
    LargeBinary,
    String,
    Text,
    create_engine,
    func,
)
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
    # Private-chat key, wrapped by the user's password (app/vault/user_keys.py).
    # Empty until the user replaces the initial password the admin set.
    chat_key_salt: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    chat_key_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    wrapped_chat_key: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
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
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatHistory(Base):
    """Attachments on a chat, deleted after 3 days by scripts/purge_history.py.

    Message text is not stored here: it stays encrypted in the checkpoints, kept
    indefinitely. `artifacts` is a list of {"type", "ref"}, where ref is the object
    key in MinIO.
    """

    __tablename__ = "chat_history"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    thread_id: Mapped[str] = mapped_column(String(128), index=True)
    artifacts: Mapped[list] = mapped_column(JSON, default=list)  # media/file refs; purged after 3d
    artifacts_purged: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
