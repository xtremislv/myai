"""Seed/read departments and expand their grants into ACL policies.

Seeding is idempotent: policy rows use a deterministic id derived from
(subject, effect, action, resource), so re-running updates in place without
creating duplicates.
"""
from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from app.db.session import ACLPolicy, Department
from app.departments.packs import DEFAULT_DEPARTMENTS, DepartmentSpec


def _policy_id(subject: str, effect: str, action: str, resource: str) -> str:
    raw = f"{subject}|{effect}|{action}|{resource}".encode()
    return hashlib.sha1(raw).hexdigest()  # 40 chars, fits String(64)


def _grants_json(spec: DepartmentSpec) -> list[dict]:
    return [{"resource": g.resource, "actions": list(g.actions)} for g in spec.grants]


def seed_departments(session: Session, specs: tuple[DepartmentSpec, ...] = DEFAULT_DEPARTMENTS) -> dict:
    """Upsert department rows and their allow-policies. Returns a small summary."""
    dept_count = 0
    policy_count = 0
    for spec in specs:
        row = session.get(Department, spec.name)
        if row is None:
            row = Department(name=spec.name)
            session.add(row)
        row.description = spec.description
        row.grants = _grants_json(spec)
        row.tools = list(spec.tools)
        row.quota_per_min = spec.quota_per_min
        row.max_concurrency = spec.max_concurrency
        row.requires_approval = spec.requires_approval
        row.critical_resources = list(spec.critical_resources)
        dept_count += 1

        for grant in spec.grants:
            for action in grant.actions:
                pid = _policy_id(spec.name, "allow", action, grant.resource)
                if session.get(ACLPolicy, pid) is None:
                    session.add(
                        ACLPolicy(
                            id=pid,
                            subject=spec.name,
                            effect="allow",
                            action=action,
                            resource=grant.resource,
                        )
                    )
                    policy_count += 1
    session.commit()
    return {"departments": dept_count, "policies_added": policy_count}


def get_department(session: Session, name: str) -> Department | None:
    return session.get(Department, name)


def list_departments(session: Session) -> list[Department]:
    return session.query(Department).all()
