"""Canonical department definitions ("policy packs") + the data-access schema.

Departments (ACL roles): account, hr, logistics, researcher, superuser, admin.

Resource type conventions (urn = "type:id"):
  accounts:*                 accounts data            -> account only
  balance_sheet:*            balance sheets           -> account only
  mail:finance/*             finance mail             -> account
  mail:career/*              career mail              -> hr
  mail:contact/*             contact mail             -> admin (members of the admin role only)
  mail:personal/<owner>/*    personal mail            -> owner only ($self)
  personal_info:<owner>/*    personal info            -> owner only ($self)
  chat_history:<owner>/*     chat history             -> owner only ($self); non-text purged after 3d
  inventory:*                inventory sheet          -> read: anyone; write: logistics, superuser
  order:* / bill:* / booking:*  orders/bills/bookings -> account (requests are forwarded here)
  payment:*                  payments                 -> account (every call needs approval)
  research:* / reports:*     research data/reports    -> researcher
  web:search|fetch|browse    web search, pages, browser -> researcher, account, logistics (browse: not logistics)
  web:commit                 browser buttons that book/pay/send -> account (every call needs approval)
  web:commit-private         the same in private chat -> everyone, for themselves (they confirm each one)
  mail:<category>/* write    send mail / mark read    -> the box's department; personal: owner
  notification:* / reminder:*  notifications/reminders -> logistics
  reminder:<owner>/*         a person's own reminders  -> owner ($self)
  files:<owner>/*            a person's own files      -> owner ($self); encrypted with their key
  shared:<department>/*      a department's shared files -> that department (delete needs approval)
  template:<department>/*    a department's document templates -> that department
  template:global/*          company-wide templates   -> read: everyone (at work); write: admin
  secret:mail/personal/*, secret:system/*  -> never the admin (people's mail logins, server keys)
  report:nsfw/*              NSFW reports             -> hr, admin

Owner-based access is modeled with the special subject "$self" and a "{owner}"
placeholder in the resource (the ACL engine substitutes the requester's id).
Department data (its mail, shared files, templates, records) is for that department's members
only. Admin gets an explicit list (platform, contact mail, admin folders, reports, credentials
other than mailbox logins), never other departments' data and never personal data.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MAIL_TOOLS = ("read_mail", "search_mail", "open_mail", "mark_mail", "send_mail", "summarize_mail")
LOOKUP_TOOLS = (
    "web_search", "news_search", "scholar_search", "product_search", "product_page", "fetch_page", "scrape_page",
    "weather",
)
BROWSE_TOOLS = (
    "browser_open", "browser_read", "browser_click", "browser_type", "browser_select",
    "browser_scroll", "browser_back", "ask_person_to_browse",
)
# Files: in private chat the person's own encrypted "My files"; at work the department's shared
# folder (shared/<department>/) and the data areas its grants reach. delete_file needs approval.
FILE_TOOLS = (
    "list_files", "read_file", "write_file", "append_file", "edit_file", "move_file", "copy_file",
    "delete_file", "restore_file",
)
REMINDER_TOOLS = ("set_reminder", "list_reminders", "cancel_reminder")
# Analysing data with code (app/sandbox.py: no network, copies of files only) and showing the
# results as a dashboard under the reply (app/dashboards.py). Everyone, private chat included.
DATA_TOOLS = ("run_python", "make_dashboard")
# Documents from templates: company templates (database) at work; in private chat the person's
# own templates (My files, templates/) and files attached to the chat.
TEMPLATE_TOOLS = ("list_templates", "read_template", "create_from_template", "save_template")
# Private chat (no department), owner's decisions of 2026-11-03: web access, plus the person's OWN
# mailbox (personal category only), files, reminders and templates, and personal shopping:
# browser_commit places an order only after the person confirms it themselves. No company data.
# The person's own saved logins, cards, UPI IDs and addresses: private chat only (owner, 2026-11-03).
WALLET_TOOLS = ("list_saved_details", "fill_saved", "ask_person_for_secret")
PRIVATE_TOOLS = (
    *LOOKUP_TOOLS, *BROWSE_TOOLS, "browser_commit", *MAIL_TOOLS, *FILE_TOOLS, *REMINDER_TOOLS, *TEMPLATE_TOOLS,
    *WALLET_TOOLS, *DATA_TOOLS,
)
# Tools that need a person's go-ahead in every department and in private chat (where the person
# confirms for themselves).
ALWAYS_CRITICAL = ("send_mail", "delete_file", "browser_commit")


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
        Grant("mail:finance/*", ("read", "list", "write")),
        Grant("order:*", ("read", "write", "list")),
        Grant("bill:*", ("read", "write", "list")),
        Grant("booking:*", ("read", "write", "list")),
        Grant("payment:*", ("read", "write", "list")),
        Grant("inventory:*", ("read", "list")),
        Grant("web:*", ("read", "write")),
        Grant("shared:account/*", ("read", "write", "list", "delete")),
        Grant("template:account/*", ("read", "write", "list", "delete")),
    ),
    tools=(
        "balance_lookup", *MAIL_TOOLS, "manage_order", "manage_bill",
        "initiate_payment", "book_service", "inventory_lookup",
        *LOOKUP_TOOLS, *BROWSE_TOOLS, "browser_commit", *FILE_TOOLS, *REMINDER_TOOLS, *TEMPLATE_TOOLS, *DATA_TOOLS,
    ),
    quota_per_min=60,
    max_concurrency=4,
    requires_approval=True,
    critical_resources=(
        "balance_sheet:*",
        "tool:initiate_payment",
        "tool:manage_bill",
        "tool:manage_order",
        "tool:book_service",
        "tool:send_mail",
        "tool:browser_commit",
    ),
)

HR = DepartmentSpec(
    name="hr",
    description="HR: career mail and NSFW reports.",
    grants=(
        Grant("mail:career/*", ("read", "list", "write")),
        Grant("report:nsfw/*", ("read", "list")),
        Grant("inventory:*", ("read", "list")),
        Grant("shared:hr/*", ("read", "write", "list", "delete")),
        Grant("template:hr/*", ("read", "write", "list", "delete")),
    ),
    tools=(*MAIL_TOOLS, "review_report", *FILE_TOOLS, *REMINDER_TOOLS, *TEMPLATE_TOOLS, *DATA_TOOLS),
)

LOGISTICS = DepartmentSpec(
    name="logistics",
    description="Logistics: inventory management, shipment tracking, notifications, reminders.",
    grants=(
        Grant("inventory:*", ("read", "write", "list")),
        Grant("shipments:*", ("read", "write", "list")),
        Grant("notification:*", ("write",)),
        Grant("reminder:*", ("write",)),
        Grant("web:search", ("read",)),
        Grant("web:fetch", ("read",)),
        Grant("shared:logistics/*", ("read", "write", "list", "delete")),
        Grant("template:logistics/*", ("read", "write", "list", "delete")),
    ),
    tools=(
        "inventory_lookup", "inventory_adjust", "track_shipment", "send_notification",
        *LOOKUP_TOOLS, *FILE_TOOLS, *REMINDER_TOOLS, *TEMPLATE_TOOLS, *DATA_TOOLS,
    ),
    quota_per_min=120,
    max_concurrency=6,
)

RESEARCHER = DepartmentSpec(
    name="researcher",
    description="Research: information gathering, synthesis, reports.",
    grants=(
        Grant("research:*", ("read", "write", "list", "delete")),
        Grant("reports:*", ("read", "list")),
        Grant("web:*", ("read",)),
        Grant("inventory:*", ("read", "list")),
        Grant("shared:researcher/*", ("read", "write", "list", "delete")),
        Grant("template:researcher/*", ("read", "write", "list", "delete")),
    ),
    tools=(
        "read_document", "list_documents", *LOOKUP_TOOLS, *BROWSE_TOOLS, "summarize", *FILE_TOOLS, *REMINDER_TOOLS, *TEMPLATE_TOOLS, *DATA_TOOLS,
    ),
    quota_per_min=120,
    max_concurrency=6,
)

SUPERUSER = DepartmentSpec(
    name="superuser",
    description="Power user: manage inventory; read non-sensitive data.",
    grants=(
        Grant("inventory:*", ("read", "write", "list")),
        Grant("shared:superuser/*", ("read", "write", "list", "delete")),
        Grant("template:superuser/*", ("read", "write", "list", "delete")),
    ),
    tools=("inventory_lookup", "inventory_adjust", *FILE_TOOLS, *REMINDER_TOOLS, *TEMPLATE_TOOLS, *DATA_TOOLS),
    quota_per_min=120,
    max_concurrency=6,
)

# Owner's decision 2026-11-04: a department's official mail and documents are for its members
# only. The admin runs the platform (people, credentials, approvals, alerts) and has the admin
# department's own mailbox (contact@) and folders, but no way into another department's data
# by being admin: an admin who should see Accounts' mail is added to Accounts like anyone else.
# Deny rules would also block an admin who IS a member (deny wins over every role), so this is
# an explicit allow list instead of "*" minus exceptions.
MAILBOX_OWNERS = {"finance": "account", "career": "hr", "contact": "admin"}
ADMIN = DepartmentSpec(
    name="admin",
    description="Runs the platform: people, credentials, approvals, alerts; the contact mailbox and "
                "the admin folders. No access to other departments' mail or documents.",
    grants=(
        Grant("system:*", ("*",)),                           # platform settings and policies
        Grant("mail:contact/*", ("read", "list", "write")),  # the admin's own (contact@) mailbox
        Grant("shared:admin/*", ("read", "write", "list", "delete")),
        Grant("template:admin/*", ("read", "write", "list", "delete")),
        Grant("template:global/*", ("read", "write", "list", "delete")),  # company-wide templates
        Grant("report:*", ("read", "list", "write")),        # explicit/illegal content reports
        Grant("inventory:*", ("read", "list")),
        Grant("web:*", ("read", "write")),
        Grant("compute:*", ("write",)),
        Grant("notification:*", ("write",)),
        Grant("object:*", ("read", "list")),                 # top-level keys with no department
        Grant("secret:*", ("read", "write", "list")),        # API keys, payment gateway...
        Grant("secret:mail/*", ("*",), effect="deny"),       # ...but no mailbox logins of any kind
        Grant("secret:system/*", ("*",), effect="deny"),     # ...nor the server's own keys
        Grant("secret:browser/*", ("*",), effect="deny"),    # ...nor saved website logins
        Grant("chat_history:*", ("*",), effect="deny"),
        Grant("personal_info:*", ("*",), effect="deny"),
        Grant("mail:personal/*", ("*",), effect="deny"),
        Grant("wallet:*", ("*",), effect="deny"),
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
        # Read-only web access for everyone, private chat included (owner's decision 2026-11-03).
        Grant("web:search", ("read",), subject="*"),
        Grant("web:fetch", ("read",), subject="*"),
        Grant("web:browse", ("read",), subject="*"),
        Grant("template:global/*", ("read", "list"), subject="*"),  # company-wide templates
        # Personal shopping in private chat: commits only after the person's own confirmation.
        Grant("web:commit-private", ("write",), subject="*"),
        # Running analysis code in the sandbox and showing dashboards.
        Grant("compute:python", ("write",), subject="*"),
        Grant("compute:dashboard", ("write",), subject="*"),
        Grant("compute:summarize", ("write",), subject="*"),
    ),
    tools=(),
)

# Owner-based access (bound to "$self"; applies to the resource's owner).
SELF = DepartmentSpec(
    name="$self",
    description="Owner-only access to one's personal data.",
    grants=(
        Grant("personal_info:{owner}/*", ("read", "write", "list"), subject="$self"),
        Grant("mail:personal/{owner}/*", ("read", "list", "write"), subject="$self"),
        Grant("chat_history:{owner}/*", ("read", "list", "delete"), subject="$self"),
        Grant("files:{owner}/*", ("read", "write", "list", "delete"), subject="$self"),
        Grant("reminder:{owner}/*", ("read", "write", "list", "delete"), subject="$self"),
        Grant("wallet:{owner}/*", ("read", "write", "list", "delete"), subject="$self"),
    ),
    tools=(),
)

# Real departments (seed into `departments` table + used for classification).
DEFAULT_DEPARTMENTS: tuple[DepartmentSpec, ...] = (ACCOUNT, HR, LOGISTICS, RESEARCHER, SUPERUSER, ADMIN)

# All specs whose grants become ACL policies (includes GLOBAL + SELF pseudo-roles).
POLICY_SPECS: tuple[DepartmentSpec, ...] = (*DEFAULT_DEPARTMENTS, GLOBAL, SELF)


def critical_tools() -> frozenset[str]:
    """Tools that always need human approval, whichever department calls them, so
    admin's "*" tool access does not skip the gate."""
    prefix = "tool:"
    return frozenset(
        c[len(prefix):]
        for spec in DEFAULT_DEPARTMENTS
        for c in spec.critical_resources
        if c.startswith(prefix)
    ) | frozenset(ALWAYS_CRITICAL)


# Tools whose use counts as finance activity. read_mail only counts for finance mail,
# so the admin's own personal mail never triggers a notice.
FINANCE_TOOLS = frozenset(
    {"balance_lookup", "manage_order", "manage_bill", "initiate_payment", "book_service", "browser_commit"}
)


def is_finance_call(tool: str, args: dict) -> bool:
    if tool in MAIL_TOOLS:
        return args.get("category") == "finance"
    return tool in FINANCE_TOOLS


def by_name(name: str) -> DepartmentSpec | None:
    return next((d for d in DEFAULT_DEPARTMENTS if d.name == name), None)
