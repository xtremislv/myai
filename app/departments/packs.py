"""Canonical department definitions ("policy packs") + the data-access schema.

Departments (ACL roles): account, hr, logistics, researcher, superuser, admin.

Resource type conventions (urn = "type:id"):
  accounts:*                 accounts data            -> account only
  balance_sheet:*            balance sheets           -> account only
  mail:finance/*             finance mail             -> account
  mail:career/*              career mail              -> hr
  mail:contact/*             contact mail             -> admin
  mail:personal/<owner>/*    personal mail            -> owner only ($self)
  personal_info:<owner>/*    personal info            -> owner only ($self)
  chat_history:<owner>/*     chat history             -> owner only ($self); non-text purged after 3d
  inventory:*                inventory sheet          -> read: anyone; write: logistics, superuser
  order:* / bill:* / booking:*  orders/bills/bookings -> account (requests are forwarded here)
  research:* / reports:*     research data/reports    -> researcher
  report:nsfw/*              NSFW reports             -> hr, admin

Owner-based access is modeled with the special subject "$self" and a "{owner}"
placeholder in the resource (the ACL engine substitutes the requester's id).
Admin gets everything EXCEPT chat history and personal data (explicit denies).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Grant:
    resource: str
    actions: tuple[str, ...]
    effect: str = "allow"           # "allow" | "deny"
    subject: str | None = None       # override: e.g. "$self" or "*"; default = department name


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


ACCOUNT = DepartmentSpec(
    name="account",
    description="Accounts: accounts data, balance sheets, finance mail, orders/bills/bookings. Payments approval-gated.",
    grants=(
        Grant("accounts:*", ("read", "write", "list")),
        Grant("balance_sheet:*", ("read", "write", "list")),
        Grant("mail:finance/*", ("read", "list")),
        Grant("order:*", ("read", "write", "list")),
        Grant("bill:*", ("read", "write", "list")),
        Grant("booking:*", ("read", "write", "list")),
        Grant("inventory:*", ("read", "list")),
    ),
    tools=("balance_lookup", "read_mail", "manage_order", "manage_bill", "book_service", "inventory_lookup"),
    quota_per_min=60,
    max_concurrency=4,
    requires_approval=True,
    critical_resources=("balance_sheet:*", "tool:initiate_payment", "tool:manage_bill"),
)

HR = DepartmentSpec(
    name="hr",
    description="HR: career mail and NSFW reports.",
    grants=(
        Grant("mail:career/*", ("read", "list")),
        Grant("report:nsfw/*", ("read", "list")),
        Grant("inventory:*", ("read", "list")),
    ),
    tools=("read_mail", "review_report"),
)

LOGISTICS = DepartmentSpec(
    name="logistics",
    description="Logistics: inventory management, shipment tracking, notifications, reminders.",
    grants=(
        Grant("inventory:*", ("read", "write", "list")),
        Grant("shipments:*", ("read", "write", "list")),
    ),
    tools=("inventory_lookup", "inventory_adjust", "track_shipment", "send_notification", "set_reminder"),
    quota_per_min=120,
    max_concurrency=6,
)

RESEARCHER = DepartmentSpec(
    name="researcher",
    description="Research: information gathering, synthesis, reports.",
    grants=(
        Grant("research:*", ("read", "write", "list")),
        Grant("reports:*", ("read", "list")),
        Grant("inventory:*", ("read", "list")),
    ),
    tools=("read_document", "list_documents", "web_search", "summarize"),
    quota_per_min=120,
    max_concurrency=6,
)

SUPERUSER = DepartmentSpec(
    name="superuser",
    description="Power user: manage inventory; read non-sensitive data.",
    grants=(
        Grant("inventory:*", ("read", "write", "list")),
    ),
    tools=("inventory_lookup", "inventory_adjust"),
    quota_per_min=120,
    max_concurrency=6,
)

ADMIN = DepartmentSpec(
    name="admin",
    description="Full control EXCEPT chat history and personal data.",
    grants=(
        Grant("*", ("*",)),                                  # everything...
        Grant("chat_history:*", ("*",), effect="deny"),      # ...except history
        Grant("personal_info:*", ("*",), effect="deny"),     # ...and personal info
        Grant("mail:personal/*", ("*",), effect="deny"),     # ...and personal mail
    ),
    tools=("*",),
    quota_per_min=600,
    max_concurrency=12,
)

# Access everyone gets (bound to the "*" principal).
GLOBAL = DepartmentSpec(
    name="*",
    description="Baseline access for every authenticated subject.",
    grants=(
        Grant("inventory:*", ("read", "list"), subject="*"),   # anyone may READ inventory
    ),
    tools=(),
)

# Owner-based access (bound to "$self"; applies to the resource's owner).
SELF = DepartmentSpec(
    name="$self",
    description="Owner-only access to one's personal data.",
    grants=(
        Grant("personal_info:{owner}/*", ("read", "write", "list"), subject="$self"),
        Grant("mail:personal/{owner}/*", ("read", "list"), subject="$self"),
        Grant("chat_history:{owner}/*", ("read", "list", "delete"), subject="$self"),
    ),
    tools=(),
)

# Real departments (seed into `departments` table + used for classification).
DEFAULT_DEPARTMENTS: tuple[DepartmentSpec, ...] = (ACCOUNT, HR, LOGISTICS, RESEARCHER, SUPERUSER, ADMIN)

# All specs whose grants become ACL policies (includes GLOBAL + SELF pseudo-roles).
POLICY_SPECS: tuple[DepartmentSpec, ...] = (*DEFAULT_DEPARTMENTS, GLOBAL, SELF)


def by_name(name: str) -> DepartmentSpec | None:
    return next((d for d in DEFAULT_DEPARTMENTS if d.name == name), None)
