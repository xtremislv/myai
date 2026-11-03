"""Private-chat attachments: encrypted with the owner's key, deleted after 3 days.

The object in MinIO and the file's name, type and size are all encrypted with the
user's chat key. Extracted text is given to the model for the current turn only and
is never written to a checkpoint, so it cannot outlive the file. (The model's replies
are chat text, and those are kept.)
"""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
import uuid

from sqlalchemy.orm import Session

from app.db.session import ChatHistory
from app.request_manager.retention import RETENTION_DAYS
from app.vault import user_keys

logger = logging.getLogger(__name__)

MAX_CONTEXT_CHARS = 24_000  # keeps room in the model's context for the conversation
MAX_PDF_PAGES = 50
_TEXT_TYPES = {"application/json", "application/xml", "application/x-yaml", "application/yaml"}
_TEXT_SUFFIXES = {".txt", ".md", ".csv", ".tsv", ".json", ".log", ".yaml", ".yml", ".xml"}


def _put(key: str, data: bytes) -> None:
    from app.storage.objects import put_system_object

    put_system_object(key, data)


def _get(key: str) -> bytes:
    from app.storage.objects import get_system_object

    return get_system_object(key)


def _remove(key: str) -> None:
    from app.storage.objects import remove_expired_object

    remove_expired_object(key)


def _object_key(owner_id: str, attachment_id: str) -> str:
    return f"chat_attachments/{owner_id}/{attachment_id}"


def _is_expired(row: ChatHistory, now: dt.datetime) -> bool:
    created = row.created_at
    if created.tzinfo is None:  # SQLite returns naive UTC
        created = created.replace(tzinfo=dt.timezone.utc)
    return created < now - dt.timedelta(days=RETENTION_DAYS)


def _describe(row: ChatHistory, key: bytes) -> dict:
    artifact = row.artifacts[0]
    meta = json.loads(user_keys.decrypt_text(key, artifact["meta"], row.id))
    return {"id": row.id, "thread_id": row.thread_id, "created_at": row.created_at, **meta}


def _live_rows(session: Session, owner_id: str, thread_id: str | None = None) -> list[ChatHistory]:
    query = session.query(ChatHistory).filter(
        ChatHistory.owner_id == owner_id, ChatHistory.artifacts_purged.is_(False)
    )
    if thread_id is not None:
        query = query.filter(ChatHistory.thread_id == thread_id)
    now = dt.datetime.now(dt.timezone.utc)
    # The hourly purge may not have run yet; an expired file must already be gone.
    return [
        r for r in query.order_by(ChatHistory.created_at).all()
        if r.artifacts and not _is_expired(r, now)
    ]


def save(
    session: Session, key: bytes, owner_id: str, thread_id: str, name: str, content_type: str, data: bytes
) -> dict:
    attachment_id = uuid.uuid4().hex
    ref = _object_key(owner_id, attachment_id)
    _put(ref, user_keys.encrypt_bytes(key, data, attachment_id))
    meta = json.dumps({"name": name, "content_type": content_type, "size": len(data)})
    row = ChatHistory(
        id=attachment_id,
        owner_id=owner_id,
        thread_id=thread_id,
        artifacts=[{"ref": ref, "meta": user_keys.encrypt_text(key, meta, attachment_id)}],
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _describe(row, key)


def list_for(session: Session, key: bytes, owner_id: str, thread_id: str | None = None) -> list[dict]:
    return [_describe(r, key) for r in _live_rows(session, owner_id, thread_id)]


def _owned(session: Session, owner_id: str, attachment_id: str) -> ChatHistory | None:
    """Another user's attachment looks exactly like a missing one."""
    row = session.get(ChatHistory, attachment_id)
    if row is None or row.owner_id != owner_id or not row.artifacts:
        return None
    return None if _is_expired(row, dt.datetime.now(dt.timezone.utc)) else row


def read(session: Session, key: bytes, owner_id: str, attachment_id: str) -> tuple[dict, bytes] | None:
    row = _owned(session, owner_id, attachment_id)
    if row is None:
        return None
    data = user_keys.decrypt_bytes(key, _get(row.artifacts[0]["ref"]), row.id)
    return _describe(row, key), data


def delete(session: Session, owner_id: str, attachment_id: str) -> bool:
    row = _owned(session, owner_id, attachment_id)
    if row is None:
        return False
    _remove(row.artifacts[0]["ref"])
    session.delete(row)
    session.commit()
    return True


def extract_text(name: str, content_type: str, data: bytes) -> str | None:
    """The file's text, or None if this kind of file cannot be read as text."""
    lower = name.lower()
    if content_type == "application/pdf" or lower.endswith(".pdf"):
        return _pdf_text(data)
    suffix = lower[lower.rfind("."):] if "." in lower else ""
    if content_type.startswith("text/") or content_type in _TEXT_TYPES or suffix in _TEXT_SUFFIXES:
        return data.decode("utf-8", errors="replace")
    return None


def _pdf_text(data: bytes) -> str | None:
    from pypdf import PdfReader

    try:
        pages = PdfReader(io.BytesIO(data)).pages[:MAX_PDF_PAGES]
        return "\n".join(page.extract_text() or "" for page in pages).strip() or None
    except Exception:  # noqa: BLE001 - a broken PDF is still a valid attachment to store
        logger.warning("could not read a PDF attachment")
        return None


def context_for_thread(session: Session, key: bytes, owner_id: str, thread_id: str) -> str | None:
    """What the model may read from this thread's live attachments, for one turn."""
    parts: list[str] = []
    remaining = MAX_CONTEXT_CHARS
    for row in _live_rows(session, owner_id, thread_id):
        meta = _describe(row, key)
        try:
            data = user_keys.decrypt_bytes(key, _get(row.artifacts[0]["ref"]), row.id)
        except Exception:  # noqa: BLE001 - one unavailable file must not block the chat
            logger.warning("could not load attachment %s", row.id)
            parts.append(f"[{meta['name']}: temporarily unavailable]")
            continue
        text = extract_text(meta["name"], meta["content_type"], data)
        if text is None:
            parts.append(f"[{meta['name']}: this file type cannot be read]")
        elif remaining <= 0:
            parts.append(f"[{meta['name']}: not included, the attachment limit was reached]")
        else:
            shown = text[:remaining]
            remaining -= len(shown)
            cut = "" if len(shown) == len(text) else "\n[truncated]"
            parts.append(f"--- {meta['name']} ---\n{shown}{cut}")
    return "\n\n".join(parts) or None
