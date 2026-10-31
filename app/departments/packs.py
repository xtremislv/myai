"""Canonical department definitions ("policy packs").

Each department is an ACL role with:
  * grants          — what resources/actions it may touch (deny-by-default elsewhere)
  * tools           — which agent tools it may call ("*" = all, admin only)
  * quota/conc.     — Request Manager throttling knobs
  * requires_approval + critical_resources — which actions pause for human approval

These are the single source of truth; `registry.seed_departments` writes them into
the `departments` table and expands `grants` into `acl_policies`.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Grant:
    resource: str                 # e.g. "object:finance/*" or "secret:payment/*"
    actions: tuple[str, ...]       # subset of read|write|delete|list|admin|*


@dataclass(frozen=True)
class DepartmentSpec:
    name: str
    description: str
    grants: tuple[Grant, ...]
    tools: tuple[str, ...]
    quota_per_min: int = 60
    max_concurrency: int = 4
    requires_approval: bool = False
    critical_resources: tuple[str, ...] = field(default_factory=tuple)


FINANCE = DepartmentSpec(
    name="finance",
    description="Transactions, invoices, reconciliation. Critical: payments are approval-gated.",
    grants=(
        Grant("object:finance/*", ("read", "write", "list")),
        Grant("object:invoices/*", ("read", "write", "list")),
        Grant("secret:payment/*", ("read",)),  # read-only; approval-gated by the RM
    ),
    tools=("create_invoice", "get_invoice", "ledger_query", "initiate_payment"),
    quota_per_min=30,
    max_concurrency=2,
    requires_approval=True,
    critical_resources=("secret:payment/*", "tool:initiate_payment"),
)

LOGISTICS = DepartmentSpec(
    name="logistics",
    description="Shipment tracking, notifications, reminders, inventory.",
    grants=(
        Grant("object:inventory/*", ("read", "write", "list")),
        Grant("object:shipments/*", ("read", "write", "list")),
    ),
    tools=("track_shipment", "send_notification", "set_reminder", "inventory_lookup", "inventory_adjust"),
    quota_per_min=120,
    max_concurrency=6,
)

RESEARCH = DepartmentSpec(
    name="research",
    description="Information gathering, synthesis, reports.",
    grants=(
        Grant("object:research/*", ("read", "write", "list")),
        Grant("object:reports/*", ("read", "list")),
    ),
    tools=("read_document", "list_documents", "web_search", "summarize"),
    quota_per_min=120,
    max_concurrency=6,
)

MISC = DepartmentSpec(
    name="misc",
    description="General, low-privilege tasks over shared documents.",
    grants=(Grant("object:shared/*", ("read", "list")),),
    tools=("read_document", "list_documents"),
    quota_per_min=60,
    max_concurrency=4,
)

ADMIN = DepartmentSpec(
    name="admin",
    description="Full control: users, policies, secrets, departments.",
    grants=(Grant("*", ("*",)),),
    tools=("*",),
    quota_per_min=600,
    max_concurrency=12,
)

DEFAULT_DEPARTMENTS: tuple[DepartmentSpec, ...] = (FINANCE, LOGISTICS, RESEARCH, MISC, ADMIN)


def by_name(name: str) -> DepartmentSpec | None:
    return next((d for d in DEFAULT_DEPARTMENTS if d.name == name), None)
