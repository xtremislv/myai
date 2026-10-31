"""Agent tools. Each tool reconstructs the Subject from state and is ACL-gated,
so an agent can never read/write a resource the caller isn't allowed to.
"""
from __future__ import annotations

from typing import Any

from app.acl.engine import ACLEngine
from app.acl.models import Subject
from app.db.session import SessionLocal
from app.storage.objects import ObjectStore
from app.vault.aesgcm_vault import AESGCMVault


def _subject(state: dict[str, Any]) -> Subject:
    return Subject(id=state.get("subject_id", "anonymous"), roles=tuple(state.get("subject_roles", [])))


def read_document(state: dict[str, Any], key: str) -> str:
    """Read a stored document by object key (ACL: object:<key> / read)."""
    subject = _subject(state)
    with SessionLocal() as session:
        store = ObjectStore(ACLEngine(session))
        try:
            return store.get(subject, key).decode("utf-8", errors="replace")
        except PermissionError as e:
            return f"ACCESS DENIED: {e}"


def list_documents(state: dict[str, Any], prefix: str = "") -> list[str]:
    """List stored documents under a prefix (ACL: object:<prefix>* / list)."""
    subject = _subject(state)
    with SessionLocal() as session:
        store = ObjectStore(ACLEngine(session))
        try:
            return store.list(subject, prefix)
        except PermissionError as e:
            return [f"ACCESS DENIED: {e}"]


def get_secret(state: dict[str, Any], name: str) -> str:
    """Fetch a secret from the vault (ACL: secret:<name> / read)."""
    from app.acl.models import Action, Resource

    subject = _subject(state)
    with SessionLocal() as session:
        acl = ACLEngine(session)
        try:
            acl.enforce(subject, Action.READ, Resource("secret", name))
        except PermissionError as e:
            return f"ACCESS DENIED: {e}"
        value = AESGCMVault(session).get(name)
        return value if value is not None else "NOT FOUND"


# Registry the agent node can dispatch against.
TOOLS = {
    "read_document": read_document,
    "list_documents": list_documents,
    "get_secret": get_secret,
}
