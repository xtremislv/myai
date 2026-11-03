"""MinIO (S3-compatible) object storage, fronted by the ACL engine.

Every operation requires an ACL decision for the key's resource (see resource_for). The
ObjectStore is the Policy Enforcement Point (PEP) for stored files.
"""
from __future__ import annotations

import io

from minio import Minio

from app.acl.engine import ACLEngine
from app.acl.models import Action, Resource, Subject
from app.config import get_settings

_settings = get_settings()


def _client() -> Minio:
    return Minio(
        _settings.minio_endpoint,
        access_key=_settings.minio_access_key,
        secret_key=_settings.minio_secret_key,
        secure=_settings.minio_secure,
    )


def ensure_bucket() -> None:
    c = _client()
    if not c.bucket_exists(_settings.minio_bucket):
        c.make_bucket(_settings.minio_bucket)


# Private-chat attachments and the retention job bypass the ACL by design. Attachments are
# checked by owner and encrypted with the owner's key (app/storage/attachments.py); retention
# acts for the system. Everything else goes through ObjectStore.
def put_system_object(key: str, data: bytes) -> None:
    _client().put_object(_settings.minio_bucket, key, io.BytesIO(data), length=len(data))


def get_system_object(key: str) -> bytes:
    resp = _client().get_object(_settings.minio_bucket, key)
    try:
        return resp.read()
    finally:
        resp.close()
        resp.release_conn()


def remove_expired_object(key: str) -> None:
    _client().remove_object(_settings.minio_bucket, key)


def resource_for(key: str, *, listing: bool = False) -> Resource:
    """The ACL resource for a document key. Department grants are written against data types
    ("research:*", "reports:*", "personal_info:<owner>/*"), so the first folder of the key is
    the type: research/q3.txt -> research:q3.txt, and listing research/ -> research:*. A key
    with no folder stays object:<key>, which only a full grant (the admin) reaches."""
    folder, sep, rest = key.partition("/")
    if not sep or not folder:
        return Resource("object", f"{key}*" if listing else key)
    return Resource(folder, f"{rest}*" if listing else rest)


class ObjectStore:
    def __init__(self, acl: ACLEngine) -> None:
        self._c = _client()
        self._acl = acl
        self._bucket = _settings.minio_bucket

    def put(self, subject: Subject, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._acl.enforce(subject, Action.WRITE, resource_for(key))
        self._c.put_object(self._bucket, key, io.BytesIO(data), length=len(data), content_type=content_type)

    def get(self, subject: Subject, key: str) -> bytes:
        self._acl.enforce(subject, Action.READ, resource_for(key))
        resp = self._c.get_object(self._bucket, key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    def delete(self, subject: Subject, key: str) -> None:
        self._acl.enforce(subject, Action.DELETE, resource_for(key))
        self._c.remove_object(self._bucket, key)

    def list(self, subject: Subject, prefix: str = "") -> list[str]:
        self._acl.enforce(subject, Action.LIST, resource_for(prefix, listing=True))
        return [o.object_name for o in self._c.list_objects(self._bucket, prefix=prefix, recursive=True)]


def system_object_size(key: str) -> int | None:
    """The object's size, or None if there is no such object."""
    from minio.error import S3Error

    try:
        return _client().stat_object(_settings.minio_bucket, key).size
    except S3Error as e:
        if e.code in ("NoSuchKey", "NoSuchObject", "NotFound"):
            return None
        raise


def list_system_objects(prefix: str) -> list[tuple[str, int, object]]:
    """(key, size, last_modified) of every object under a prefix. No ACL: callers check."""
    return [
        (o.object_name, o.size or 0, o.last_modified)
        for o in _client().list_objects(_settings.minio_bucket, prefix=prefix, recursive=True)
    ]
