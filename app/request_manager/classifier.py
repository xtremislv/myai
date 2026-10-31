"""Resolve which department a request belongs to.

Priority:
  1. explicit department (if the subject actually holds that role, or is admin)
  2. the subject's single department role
  3. keyword match among the subject's roles
  4. fall back to 'misc' if the subject has it
Otherwise the request is unroutable (the router rejects it).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.acl.models import Subject
from app.departments.packs import DEFAULT_DEPARTMENTS

KNOWN = {d.name for d in DEFAULT_DEPARTMENTS}

# Lightweight keyword hints (heuristic; the authoritative signal is the role).
KEYWORDS: dict[str, tuple[str, ...]] = {
    "finance": ("invoice", "payment", "pay ", "transaction", "transfer", "refund", "ledger", "reconcile"),
    "logistics": ("shipment", "ship ", "track", "inventory", "stock", "reminder", "notify", "delivery"),
    "research": ("research", "summar", "report", "analyze", "compare", "literature"),
}


@dataclass(frozen=True)
class Classification:
    department: str | None
    reason: str


def classify(subject: Subject, message: str, explicit: str | None = None) -> Classification:
    roles = set(subject.roles)
    is_admin = "admin" in roles

    # 1. explicit department
    if explicit:
        if explicit not in KNOWN:
            return Classification(None, f"unknown department '{explicit}'")
        if explicit in roles or is_admin:
            return Classification(explicit, "explicit department")
        return Classification(None, f"not a member of '{explicit}'")

    # Department roles the subject actually has (admin can act for any).
    member_depts = KNOWN & roles if not is_admin else KNOWN
    non_admin = member_depts - {"admin"}

    # 2. single department role
    if len(non_admin) == 1:
        return Classification(next(iter(non_admin)), "sole department role")

    # 3. keyword match among the subject's departments
    text = message.lower()
    for dept, words in KEYWORDS.items():
        if dept in member_depts and any(w in text for w in words):
            return Classification(dept, f"keyword match: {dept}")

    # 4. misc fallback
    if "misc" in member_depts:
        return Classification("misc", "misc fallback")
    if is_admin:
        return Classification("admin", "admin default")

    return Classification(None, "no department could be resolved")
