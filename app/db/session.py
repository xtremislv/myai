"""SQLAlchemy engine/session + ORM models for users, ACL, and vault."""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    LargeBinary,
    String,
    create_engine,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

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
    Base.metadata.create_all(engine)
