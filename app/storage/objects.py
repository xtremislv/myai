"""MinIO (S3-compatible) object storage, fronted by the ACL engine.

Every operation requires an ACL decision for resource 'object:<key>'. The
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


class ObjectStore:
    def __init__(self, acl: ACLEngine) -> None:
        self._c = _client()
        self._acl = acl
        self._bucket = _settings.minio_bucket

    def put(self, subject: Subject, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._acl.enforce(subject, Action.WRITE, Resource("object", key))
        self._c.put_object(self._bucket, key, io.BytesIO(data), length=len(data), content_type=content_type)

    def get(self, subject: Subject, key: str) -> bytes:
        self._acl.enforce(subject, Action.READ, Resource("object", key))
        resp = self._c.get_object(self._bucket, key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    def delete(self, subject: Subject, key: str) -> None:
        self._acl.enforce(subject, Action.DELETE, Resource("object", key))
        self._c.remove_object(self._bucket, key)

    def list(self, subject: Subject, prefix: str = "") -> list[str]:
        self._acl.enforce(subject, Action.LIST, Resource("object", f"{prefix}*"))
        return [o.object_name for o in self._c.list_objects(self._bucket, prefix=prefix, recursive=True)]
