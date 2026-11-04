"""Company files that department agents can manage, through the ACL-checked ObjectStore.

A path whose first folder is a known data area ("research/q3.md", "reports/...",
"shared/account/...") is used as is; anything else lands in the department's own shared
folder: "budget.csv" from account work is shared/account/budget.csv. The ACL decides every
read, write, list and delete (grants in app/departments/packs.py).

Before a file is overwritten or deleted, its old content is copied to _versions/<key>/<time>,
so restore_file can undo the change. Those copies are removed after VERSION_DAYS.
"""
from __future__ import annotations

import datetime as dt
import posixpath

from app.acl.engine import ACLEngine
from app.acl.models import Action, Subject
from app.storage import objects
from app.storage.userfiles import FileError, guess_type

KNOWN_AREAS = frozenset({"shared", "research", "reports", "accounts", "balance_sheet"})
VERSIONS = "_versions/"
VERSION_DAYS = 30
# Object prefixes that hold other things (encrypted attachments and personal files, undo copies).
HIDDEN = ("_versions/", "userfiles/", "chat_attachments/")


def key_for(path: str, department: str) -> str:
    raw = (path or "").replace("\\", "/").strip().strip("/")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise FileError(f"'{path}' is not a valid file path")
    if not parts:
        return f"shared/{department}/"
    clean = posixpath.join(*parts)
    if parts[0] in KNOWN_AREAS and len(parts) > 1:
        return clean
    return f"shared/{department}/{clean}"


def folder_for(path: str, department: str) -> str:
    key = key_for(path, department)
    return key if key.endswith("/") else key + "/"


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _keep_old(key: str) -> bool:
    """Copy the current content aside before it changes. False if there was none."""
    if objects.system_object_size(key) is None:
        return False
    objects.put_system_object(f"{VERSIONS}{key}/{_stamp()}", objects.get_system_object(key))
    return True


class DeptFiles:
    def __init__(self, acl: ACLEngine, subject: Subject, department: str) -> None:
        self._acl = acl
        self._subject = subject
        self._dept = department
        self._store = objects.ObjectStore(acl)

    def list(self, folder: str = "") -> list[tuple[str, int]]:
        prefix = folder_for(folder, self._dept)
        self._acl.enforce(self._subject, Action.LIST, objects.resource_for(prefix, listing=True))
        return [(k, size) for k, size, _ in objects.list_system_objects(prefix) if not k.startswith(HIDDEN)]

    def entries(self, folder: str = "") -> list[tuple[str, int, object]]:
        """Like list, with each file's last change time."""
        prefix = folder_for(folder, self._dept)
        self._acl.enforce(self._subject, Action.LIST, objects.resource_for(prefix, listing=True))
        return [(k, size, when) for k, size, when in objects.list_system_objects(prefix) if not k.startswith(HIDDEN)]

    def read(self, path: str) -> tuple[str, bytes]:
        key = key_for(path, self._dept)
        self._acl.enforce(self._subject, Action.READ, objects.resource_for(key))
        if objects.system_object_size(key) is None:
            raise FileError(f"there is no file '{key}'")
        return key, self._store.get(self._subject, key)

    def write(self, path: str, data: bytes, *, overwrite: bool = True) -> tuple[str, bool]:
        key = key_for(path, self._dept)
        if key.endswith("/"):
            raise FileError("give a file name, e.g. notes/plan.md")
        self._acl.enforce(self._subject, Action.WRITE, objects.resource_for(key))
        existed = objects.system_object_size(key) is not None
        if existed and not overwrite:
            raise FileError(f"'{key}' already exists")
        if existed:
            _keep_old(key)
        self._store.put(self._subject, key, data, guess_type(key))
        return key, not existed

    def delete(self, path: str) -> str:
        key = key_for(path, self._dept)
        self._acl.enforce(self._subject, Action.DELETE, objects.resource_for(key))
        if not _keep_old(key):
            raise FileError(f"there is no file '{key}'")
        self._store.delete(self._subject, key)
        return key

    def restore(self, path: str) -> str:
        """Put back the content from before the last change or delete."""
        key = key_for(path, self._dept)
        self._acl.enforce(self._subject, Action.WRITE, objects.resource_for(key))
        copies = sorted(k for k, _, _ in objects.list_system_objects(f"{VERSIONS}{key}/"))
        if not copies:
            raise FileError(f"there is no earlier version of '{key}'")
        data = objects.get_system_object(copies[-1])
        _keep_old(key)
        self._store.put(self._subject, key, data, guess_type(key))
        objects.remove_expired_object(copies[-1])
        return key


def purge_versions(now: dt.datetime | None = None, days: int = VERSION_DAYS) -> int:
    cutoff = (now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(days=days)
    old = [k for k, _, modified in objects.list_system_objects(VERSIONS) if modified and modified < cutoff]
    for key in old:
        objects.remove_expired_object(key)
    return len(old)
