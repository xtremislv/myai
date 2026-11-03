"""A person's own files ("My files"): encrypted with their chat key, versioned, with a trash.

Paths, and every stored version of the content, are encrypted with the owner's key, so only
the owner (signed in, or through their private chat) can open them; the admin can't, and
neither can department work, which runs without the person's key.

Each write keeps the previous version (up to MAX_VERSIONS), so an agent's edit can be undone.
A deleted file goes to the trash for TRASH_DAYS and can be restored until then.
"""
from __future__ import annotations

import datetime as dt
import posixpath
import uuid
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from sqlalchemy.orm import Session

from app.db.session import UserFile
from app.vault import user_keys

MAX_VERSIONS = 5
TRASH_DAYS = 7
MAX_PATH = 300
QUOTA_BYTES = 500 * 1024 * 1024


class FileError(ValueError):
    """A problem the person (or the model) can fix: the message says how."""


@dataclass
class FileInfo:
    id: str
    path: str
    size: int
    content_type: str
    version: int
    versions: int
    updated_at: dt.datetime
    deleted_at: dt.datetime | None

    def as_dict(self) -> dict:
        return {
            "id": self.id, "path": self.path, "size": self.size, "content_type": self.content_type,
            "version": self.version, "versions": self.versions, "updated_at": self.updated_at,
            "deleted_at": self.deleted_at,
        }


# ---- object storage (patched in tests) ----
def _put(key: str, data: bytes) -> None:
    from app.storage.objects import put_system_object

    put_system_object(key, data)


def _get(key: str) -> bytes:
    from app.storage.objects import get_system_object

    return get_system_object(key)


def _remove(key: str) -> None:
    from app.storage.objects import remove_expired_object

    remove_expired_object(key)


def _blob_key(owner_id: str, file_id: str, version: int) -> str:
    return f"userfiles/{owner_id}/{file_id}/{version}"


def _blob_context(file_id: str, version: int) -> str:
    return f"file:{file_id}:{version}"


def _path_context(owner_id: str, file_id: str) -> str:
    return f"{owner_id}:file:{file_id}"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is not None and value.tzinfo is None:  # SQLite returns naive UTC
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def normalize(path: str) -> str:
    """A clean relative path: "notes//todo.md" -> "notes/todo.md". No "..", no empty names."""
    raw = (path or "").replace("\\", "/").strip().strip("/")
    if not raw:
        raise FileError("give a file name, e.g. notes/todo.md")
    parts = [p.strip() for p in raw.split("/") if p.strip() not in ("", ".")]
    if any(p == ".." for p in parts) or not parts:
        raise FileError(f"'{path}' is not a valid file path")
    clean = posixpath.join(*parts)
    if len(clean) > MAX_PATH or any(ord(c) < 32 for c in clean):
        raise FileError("that file name is too long or has control characters")
    return clean


def guess_type(path: str) -> str:
    import mimetypes

    return mimetypes.guess_type(path)[0] or ("text/plain" if "." not in path.rsplit("/", 1)[-1] else "application/octet-stream")


# ---- reading the index ----
def _rows(session: Session, key: bytes, owner_id: str, *, deleted: bool | None = False) -> list[tuple[UserFile, str]]:
    query = session.query(UserFile).filter(UserFile.owner_id == owner_id)
    if deleted is True:
        query = query.filter(UserFile.deleted_at.is_not(None))
    elif deleted is False:
        query = query.filter(UserFile.deleted_at.is_(None))
    out = []
    for row in query.all():
        try:
            out.append((row, user_keys.decrypt_text(key, row.encrypted_path, _path_context(owner_id, row.id))))
        except InvalidTag:
            continue  # written under a key nobody can unlock any more
    return out


def _info(row: UserFile, path: str) -> FileInfo:
    return FileInfo(
        id=row.id, path=path, size=row.size, content_type=row.content_type, version=row.version,
        versions=len(row.versions or []), updated_at=_aware(row.updated_at) or _now(),
        deleted_at=_aware(row.deleted_at),
    )


def _find(session: Session, key: bytes, owner_id: str, path: str, *, deleted: bool = False) -> tuple[UserFile, str] | None:
    path = normalize(path)
    matches = [(r, p) for r, p in _rows(session, key, owner_id, deleted=deleted) if p == path]
    if deleted:  # the most recently deleted copy
        matches.sort(key=lambda rp: _aware(rp[0].deleted_at) or _now(), reverse=True)
    return matches[0] if matches else None


def by_id(session: Session, key: bytes, owner_id: str, file_id: str) -> FileInfo | None:
    row = session.get(UserFile, file_id)
    if row is None or row.owner_id != owner_id:
        return None
    try:
        path = user_keys.decrypt_text(key, row.encrypted_path, _path_context(owner_id, row.id))
    except InvalidTag:
        return None
    return _info(row, path)


def list_files(session: Session, key: bytes, owner_id: str, folder: str = "", *, trash: bool = False) -> list[FileInfo]:
    prefix = normalize(folder) + "/" if folder.strip().strip("/") else ""
    items = [_info(r, p) for r, p in _rows(session, key, owner_id, deleted=trash) if p.startswith(prefix)]
    return sorted(items, key=lambda f: f.path.lower())


def used_bytes(session: Session, owner_id: str) -> int:
    rows = session.query(UserFile).filter(UserFile.owner_id == owner_id).all()
    return sum(v.get("size", 0) for r in rows for v in (r.versions or []))


# ---- content ----
def read(session: Session, key: bytes, owner_id: str, path: str) -> tuple[FileInfo, bytes]:
    found = _find(session, key, owner_id, path)
    if found is None:
        raise FileError(f"there is no file '{normalize(path)}' in your files")
    row, clean = found
    return _info(row, clean), _read_version(key, owner_id, row, row.version)


def read_by_id(session: Session, key: bytes, owner_id: str, file_id: str) -> tuple[FileInfo, bytes] | None:
    info = by_id(session, key, owner_id, file_id)
    if info is None:
        return None
    row = session.get(UserFile, file_id)
    return info, _read_version(key, owner_id, row, row.version)


def _read_version(key: bytes, owner_id: str, row: UserFile, version: int) -> bytes:
    return user_keys.decrypt_bytes(key, _get(_blob_key(owner_id, row.id, version)), _blob_context(row.id, version))


def write(
    session: Session, key: bytes, owner_id: str, path: str, data: bytes,
    content_type: str | None = None, *, overwrite: bool = True, limit: int | None = None,
) -> tuple[FileInfo, bool]:
    """Create or replace a file, keeping the old version. Returns (info, created)."""
    from app.config import get_settings

    path = normalize(path)
    limit = limit or get_settings().attachment_max_bytes
    if len(data) > limit:
        raise FileError(f"files are limited to {limit // (1024 * 1024)} MB")
    if used_bytes(session, owner_id) + len(data) > QUOTA_BYTES:
        raise FileError("your files are full: delete some first")
    found = _find(session, key, owner_id, path)
    now = _now()
    if found is None:
        row = UserFile(
            id=uuid.uuid4().hex, owner_id=owner_id, content_type=content_type or guess_type(path),
            size=len(data), version=1, versions=[], created_at=now, updated_at=now,
        )
        row.encrypted_path = user_keys.encrypt_text(key, path, _path_context(owner_id, row.id))
        created = True
        session.add(row)
    else:
        if not overwrite:
            raise FileError(f"'{path}' already exists")
        row, _ = found
        row.version += 1
        row.size = len(data)
        row.updated_at = now
        if content_type:
            row.content_type = content_type
        created = False
    _put(_blob_key(owner_id, row.id, row.version), user_keys.encrypt_bytes(key, data, _blob_context(row.id, row.version)))
    versions = [*(row.versions or []), {"v": row.version, "size": len(data), "at": now.isoformat()}]
    for old in versions[:-MAX_VERSIONS]:
        _remove(_blob_key(owner_id, row.id, old["v"]))
    row.versions = versions[-MAX_VERSIONS:]
    session.commit()
    return _info(row, path), created


def delete(session: Session, owner_id: str, key: bytes, path: str) -> FileInfo:
    """Move to the trash. restore() brings it back for TRASH_DAYS."""
    found = _find(session, key, owner_id, path)
    if found is None:
        raise FileError(f"there is no file '{normalize(path)}' in your files")
    row, clean = found
    row.deleted_at = _now()
    session.commit()
    return _info(row, clean)


def delete_by_id(session: Session, owner_id: str, file_id: str) -> bool:
    row = session.get(UserFile, file_id)
    if row is None or row.owner_id != owner_id or row.deleted_at is not None:
        return False
    row.deleted_at = _now()
    session.commit()
    return True


def undelete_by_id(session: Session, owner_id: str, file_id: str) -> bool:
    row = session.get(UserFile, file_id)
    if row is None or row.owner_id != owner_id or row.deleted_at is None:
        return False
    row.deleted_at = None
    session.commit()
    return True


def restore(session: Session, key: bytes, owner_id: str, path: str) -> str:
    """Undo: bring a file back from the trash, or else return it to its previous version."""
    clean = normalize(path)
    trashed = _find(session, key, owner_id, clean, deleted=True)
    live = _find(session, key, owner_id, clean)
    if trashed is not None and live is None:
        trashed[0].deleted_at = None
        session.commit()
        return f"Restored '{clean}' from the trash."
    if live is None:
        raise FileError(f"there is no file '{clean}' to restore")
    row, _ = live
    if len(row.versions or []) < 2:
        raise FileError(f"'{clean}' has no earlier version")
    previous = row.versions[-2]
    data = _read_version(key, owner_id, row, previous["v"])
    write(session, key, owner_id, clean, data, row.content_type)
    return f"'{clean}' is back to how it was before the last change (a new version, so this can be undone too)."


def move(session: Session, key: bytes, owner_id: str, src: str, dst: str) -> str:
    src, dst = normalize(src), normalize(dst)
    found = _find(session, key, owner_id, src)
    if found is None:
        raise FileError(f"there is no file '{src}' in your files")
    if _find(session, key, owner_id, dst) is not None:
        raise FileError(f"'{dst}' already exists")
    row, _ = found
    row.encrypted_path = user_keys.encrypt_text(key, dst, _path_context(owner_id, row.id))
    row.updated_at = _now()
    session.commit()
    return f"Moved '{src}' to '{dst}'."


def copy(session: Session, key: bytes, owner_id: str, src: str, dst: str) -> str:
    info, data = read(session, key, owner_id, src)
    write(session, key, owner_id, dst, data, info.content_type, overwrite=False)
    return f"Copied '{info.path}' to '{normalize(dst)}'."


# ---- housekeeping ----
def _drop(session: Session, row: UserFile) -> None:
    for v in row.versions or []:
        try:
            _remove(_blob_key(row.owner_id, row.id, v["v"]))
        except Exception:  # noqa: BLE001 - a missing object is already gone
            pass
    session.delete(row)


def purge_trash(session: Session, now: dt.datetime | None = None, days: int = TRASH_DAYS) -> int:
    cutoff = (now or _now()) - dt.timedelta(days=days)
    rows = [r for r in session.query(UserFile).filter(UserFile.deleted_at.is_not(None)).all()
            if _aware(r.deleted_at) < cutoff]
    for row in rows:
        _drop(session, row)
    session.commit()
    return len(rows)


def erase_owner(session: Session, owner_id: str) -> int:
    rows = session.query(UserFile).filter(UserFile.owner_id == owner_id).all()
    for row in rows:
        _drop(session, row)
    session.commit()
    return len(rows)
