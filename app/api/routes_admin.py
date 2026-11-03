"""Admin API: manage ACL policies and vault secrets (all admin-gated)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.db.session import ACLPolicy, get_session
from app.vault.aesgcm_vault import AESGCMVault

router = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])


# ---- ACL policies ----
class PolicyIn(BaseModel):
    subject: str          # role, user id, or "*"
    effect: str           # "allow" | "deny"
    action: str           # read|write|delete|list|admin|*
    resource: str         # e.g. "object:reports/*"


@router.get("/policies")
def list_policies(session: Session = Depends(get_session)) -> list[dict]:
    return [
        {"id": p.id, "subject": p.subject, "effect": p.effect, "action": p.action, "resource": p.resource}
        for p in session.query(ACLPolicy).all()
    ]


@router.post("/policies", status_code=status.HTTP_201_CREATED)
def add_policy(body: PolicyIn, session: Session = Depends(get_session)) -> dict:
    p = ACLPolicy(id=uuid.uuid4().hex, **body.model_dump())
    session.add(p)
    session.commit()
    return {"id": p.id}


@router.delete("/policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_policy(policy_id: str, session: Session = Depends(get_session)) -> None:
    p = session.get(ACLPolicy, policy_id)
    if p:
        session.delete(p)
        session.commit()


# ---- Vault secrets ----
class SecretIn(BaseModel):
    name: str
    value: str


# The server's own keys, and the mailbox logins people connect themselves (My account), are not
# the admin's to read, change or remove.
PROTECTED_PREFIXES = ("system/", "mail/personal/", "browser/")


def _not_protected(name: str) -> None:
    if name.startswith(PROTECTED_PREFIXES):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "that credential is managed by the system or by its owner")


@router.get("/secrets")
def list_secrets(session: Session = Depends(get_session)) -> list[str]:
    return [n for n in AESGCMVault(session).list_names() if not n.startswith(PROTECTED_PREFIXES)]


@router.put("/secrets", status_code=status.HTTP_204_NO_CONTENT)
def put_secret(body: SecretIn, session: Session = Depends(get_session)) -> None:
    _not_protected(body.name)
    AESGCMVault(session).put(body.name, body.value)


@router.delete("/secrets/{name:path}", status_code=status.HTTP_204_NO_CONTENT)
def delete_secret(name: str, session: Session = Depends(get_session)) -> None:
    _not_protected(name)
    AESGCMVault(session).delete(name)
