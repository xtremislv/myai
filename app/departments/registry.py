"""Seed/read departments and expand their grants into ACL policies.

- Real departments (DEFAULT_DEPARTMENTS) get a row in the `departments` table.
- All specs (incl. the "*" global and "$self" owner pseudo-roles) have their
  grants expanded into `acl_policies`.
Seeding is idempotent: policy rows use a deterministic id derived from
(subject, effect, action, resource).
"""
from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from app.db.session import ACLPolicy, Department
from app.departments.packs import DEFAULT_DEPARTMENTS, POLICY_SPECS, DepartmentSpec


def _policy_id(subject: str, effect: str, action: str, resource: str) -> str:
    raw = f"{subject}|{effect}|{action}|{resource}".encode()
    return hashlib.sha1(raw).hexdigest()


def _grants_json(spec: DepartmentSpec) -> list[dict]:
    return [{"resource": g.resource, "actions": list(g.actions), "effect": g.effect} for g in spec.grants]


def seed_departments(session: Session) -> dict:
    """Upsert department rows and expand all policy specs. Returns a summary."""
    dept_count = 0
    policy_count = 0

    # 1. department-table rows (real departments only)
    for spec in DEFAULT_DEPARTMENTS:
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

    # 2. ACL policies (all specs, incl. "*" and "$self")
    wanted: set[str] = set()
    for spec in POLICY_SPECS:
        for grant in spec.grants:
            subject = grant.subject or spec.name
            for action in grant.actions:
                pid = _policy_id(subject, grant.effect, action, grant.resource)
                wanted.add(pid)
                if session.get(ACLPolicy, pid) is None:
                    session.add(
                        ACLPolicy(
                            id=pid,
                            subject=subject,
                            effect=grant.effect,
                            action=action,
                            resource=grant.resource,
                        )
                    )
                    policy_count += 1
    # 3. Built-in policies packs.py no longer has (e.g. the admin's old "*" grant, removed
    # 2026-11-04) must go, or access would never shrink. A built-in policy's id is the hash of its
    # own fields; hand-made ones (admin API) have random ids and are left alone.
    removed = 0
    for row in session.query(ACLPolicy).all():
        built_in = row.id == _policy_id(row.subject, row.effect, row.action, row.resource)
        if built_in and row.id not in wanted:
            session.delete(row)
            removed += 1
    session.commit()
    return {"departments": dept_count, "policies_added": policy_count, "policies_removed": removed}


def get_department(session: Session, name: str) -> Department | None:
    return session.get(Department, name)


def list_departments(session: Session) -> list[Department]:
    return session.query(Department).all()
