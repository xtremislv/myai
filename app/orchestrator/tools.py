"""Agent tools + per-department tool registries.

Each tool reconstructs the Subject from state and is ACL-gated against the new
data schema (see app/departments/packs.py), so an agent can never reach a
resource its caller isn't allowed to — even if the LLM asks.

External connectors (payment gateway, email/SMS, ERP, web search) are STUBS:
the interface + ACL gate are real; wiring the live backend is deferred until
credentials exist. Stubs are clearly marked "[stub]".
"""
from __future__ import annotations

import inspect
from typing import Any, Callable

from app.acl.engine import ACLEngine
from app.acl.models import Action, Resource, Subject
from app.db.session import SessionLocal
from app.storage.objects import ObjectStore
from app.vault.aesgcm_vault import AESGCMVault

_ACTIONS = {"read": Action.READ, "write": Action.WRITE, "list": Action.LIST, "delete": Action.DELETE}


def _subject(state: dict[str, Any]) -> Subject:
    return Subject(id=state.get("subject_id", "anonymous"), roles=tuple(state.get("subject_roles", [])))


def _enforce(state: dict[str, Any], action: str, rtype: str, rid: str) -> str | None:
    """Return an error string if denied, else None."""
    with SessionLocal() as session:
        try:
            ACLEngine(session).enforce(_subject(state), _ACTIONS[action], Resource(rtype, rid))
        except PermissionError as e:
            return f"ACCESS DENIED: {e}"
    return None


# ---- document/object tools ----
def read_document(state: dict[str, Any], key: str) -> str:
    subject = _subject(state)
    with SessionLocal() as session:
        store = ObjectStore(ACLEngine(session))
        try:
            return store.get(subject, key).decode("utf-8", errors="replace")
        except PermissionError as e:
            return f"ACCESS DENIED: {e}"


def list_documents(state: dict[str, Any], prefix: str = "") -> list[str]:
    subject = _subject(state)
    with SessionLocal() as session:
        store = ObjectStore(ACLEngine(session))
        try:
            return store.list(subject, prefix)
        except PermissionError as e:
            return [f"ACCESS DENIED: {e}"]


def get_secret(state: dict[str, Any], name: str) -> str:
    if (err := _enforce(state, "read", "secret", name)) is not None:
        return err
    with SessionLocal() as session:
        value = AESGCMVault(session).get(name)
        return value if value is not None else "NOT FOUND"


# ---- account tools ----
def balance_lookup(state: dict[str, Any], account: str) -> str:
    if (err := _enforce(state, "read", "balance_sheet", account)) is not None:
        return err
    return f"[stub] balance sheet for {account}: (connect ledger/ERP)"


def manage_order(state: dict[str, Any], order_id: str = "new", detail: str = "") -> str:
    if (err := _enforce(state, "write", "order", order_id)) is not None:
        return err
    return f"[stub] order {order_id} recorded: {detail} (connect order system)"


def manage_bill(state: dict[str, Any], bill_id: str = "new", detail: str = "") -> str:
    if (err := _enforce(state, "write", "bill", bill_id)) is not None:
        return err
    return f"[stub] bill {bill_id} recorded: {detail} (connect billing)"


def initiate_payment(
    state: dict[str, Any], payee: str, amount: float, currency: str = "INR", reference: str = ""
) -> str:
    if (err := _enforce(state, "write", "payment", payee)) is not None:
        return err
    return f"[stub] payment of {amount} {currency} to {payee} ({reference}) (connect payment gateway)"


def book_service(state: dict[str, Any], service: str, detail: str = "") -> str:
    if (err := _enforce(state, "write", "booking", service)) is not None:
        return err
    return f"[stub] service '{service}' booked: {detail} (connect provider)"


def read_mail(state: dict[str, Any], category: str, box: str = "") -> str:
    # Personal mail is always the caller's own: the owner is never LLM-supplied.
    owner = f"{_subject(state).id}/" if category == "personal" else ""
    rid = f"{category}/{owner}{box or 'inbox'}"
    if (err := _enforce(state, "read", "mail", rid)) is not None:
        return err
    return f"[stub] {category} mailbox '{box or 'inbox'}' (connect mail server)"


# ---- logistics tools ----
def inventory_lookup(state: dict[str, Any], sku: str = "") -> str:
    if (err := _enforce(state, "read", "inventory", sku or "*")) is not None:
        return err
    return f"[stub] inventory for {sku or 'all SKUs'} (connect inventory sheet)"


def inventory_adjust(state: dict[str, Any], sku: str, delta: int) -> str:
    if (err := _enforce(state, "write", "inventory", sku)) is not None:
        return err
    return f"[stub] inventory {sku} adjusted by {delta} (connect inventory sheet)"


def track_shipment(state: dict[str, Any], shipment_id: str) -> str:
    if (err := _enforce(state, "read", "shipments", shipment_id)) is not None:
        return err
    return f"[stub] shipment {shipment_id} status (connect carrier API)"


def send_notification(state: dict[str, Any], to: str, text: str) -> str:
    if (err := _enforce(state, "write", "notification", "send")) is not None:
        return err
    return f"[stub] notification to {to}: {text} (connect email/SMS provider)"


def set_reminder(state: dict[str, Any], when: str, text: str) -> str:
    if (err := _enforce(state, "write", "reminder", "set")) is not None:
        return err
    return f"[stub] reminder set for {when}: {text} (connect scheduler)"


# ---- research tools ----
def web_search(state: dict[str, Any], query: str) -> str:
    if (err := _enforce(state, "read", "web", "search")) is not None:
        return err
    return f"[stub] web search for '{query}' (connect search API)"


def summarize(state: dict[str, Any], text: str) -> str:
    if (err := _enforce(state, "read", "research", "summarize")) is not None:
        return err
    return f"[stub] summary of {len(text)} chars (agent will summarize inline)"


# ---- hr tools ----
def review_report(state: dict[str, Any], report_id: str) -> str:
    if (err := _enforce(state, "read", "report", f"nsfw/{report_id}")) is not None:
        return err
    return f"[stub] NSFW report {report_id} (see /v1/reports)"


# Full tool table.
TOOLS: dict[str, Callable[..., Any]] = {
    "read_document": read_document,
    "list_documents": list_documents,
    "get_secret": get_secret,
    "balance_lookup": balance_lookup,
    "manage_order": manage_order,
    "manage_bill": manage_bill,
    "initiate_payment": initiate_payment,
    "book_service": book_service,
    "read_mail": read_mail,
    "inventory_lookup": inventory_lookup,
    "inventory_adjust": inventory_adjust,
    "track_shipment": track_shipment,
    "send_notification": send_notification,
    "set_reminder": set_reminder,
    "web_search": web_search,
    "summarize": summarize,
    "review_report": review_report,
}

# Per-department tool allow-lists. "*" = all tools.
DEPARTMENT_TOOLS: dict[str, tuple[str, ...]] = {
    "account": (
        "balance_lookup", "read_mail", "manage_order", "manage_bill",
        "initiate_payment", "book_service", "inventory_lookup",
    ),
    "hr": ("read_mail", "review_report"),
    "logistics": ("inventory_lookup", "inventory_adjust", "track_shipment", "send_notification", "set_reminder"),
    "researcher": ("read_document", "list_documents", "web_search", "summarize"),
    "superuser": ("inventory_lookup", "inventory_adjust"),
    "admin": ("*",),
}


def tool_signatures(department: str | None) -> list[str]:
    """How to call each of a department's tools, for the agent's prompt. "?" marks an
    optional argument."""

    def describe(fn: Callable[..., Any]) -> str:
        params = list(inspect.signature(fn).parameters.values())[1:]  # the first is state
        return ", ".join(p.name + ("?" if p.default is not inspect.Parameter.empty else "") for p in params)

    return [f"{name}({describe(fn)})" for name, fn in tools_for(department).items()]


def tools_for(department: str | None) -> dict[str, Callable[..., Any]]:
    """The tool subset an agent in `department` may call (ACL still applies)."""
    names = DEPARTMENT_TOOLS.get(department or "", ())
    if names == ("*",):
        return dict(TOOLS)
    return {n: TOOLS[n] for n in names if n in TOOLS}
