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
import typing
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


# What each tool is for, shown to the model with its schema. Keep them short and concrete:
# the model picks tools by these words.
TOOL_DESCRIPTIONS: dict[str, str] = {
    "read_document": "Read one company document by its full key, e.g. research/q3-notes.txt.",
    "list_documents": "List company document keys that start with a prefix, e.g. research/ or reports/.",
    "get_secret": "Read a stored credential from the vault by name.",
    "balance_lookup": "Look up the company ledger balance for an account id, e.g. ACC-12.",
    "manage_order": "Create (order_id 'new') or update a company order, with details.",
    "manage_bill": "Create (bill_id 'new') or update a company bill or invoice, with details.",
    "initiate_payment": "Pay a payee from company funds. Always needs an approver.",
    "book_service": "Book or subscribe the company to a service.",
    "read_mail": "Read a company mailbox: category finance, career, contact or personal.",
    "inventory_lookup": "Check stock on hand for a SKU, or for everything if sku is empty.",
    "inventory_adjust": "Change stock for a SKU by delta units (negative removes stock).",
    "track_shipment": "Get the status and expected arrival of a shipment by id, e.g. SHP-8842.",
    "send_notification": "Send a short notification to a person or team.",
    "set_reminder": "Set a reminder: when (e.g. 'Monday 09:00') and what text.",
    "web_search": "Search the web for public information.",
    "summarize": "Summarize a long text.",
    "review_report": "Open a content report by id.",
}

_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}
_BY_NAME = {"str": str, "int": int, "float": float, "bool": bool}


def _hints(fn: Callable[..., Any]) -> dict[str, Any]:
    """Argument types. Falls back to reading the annotation text when a hint can't be
    evaluated (e.g. a tool defined where Any isn't imported): only simple types matter here."""
    try:
        return typing.get_type_hints(fn)
    except (NameError, TypeError):
        raw = getattr(fn, "__annotations__", {})
        return {k: _BY_NAME.get(v, v) if isinstance(v, str) else v for k, v in raw.items()}


def _parameters(fn: Callable[..., Any]) -> dict[str, Any]:
    """JSON Schema for a tool's arguments, from its signature (the first, state, is left out)."""
    hints = _hints(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in list(inspect.signature(fn).parameters.values())[1:]:
        schema: dict[str, Any] = {"type": _JSON_TYPES.get(hints.get(param.name), "string")}
        if param.default is inspect.Parameter.empty:
            required.append(param.name)
        else:
            schema["default"] = param.default
        properties[param.name] = schema
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def tool_schemas(department: str | None) -> list[dict[str, Any]]:
    """The department's tools in the OpenAI function-calling format, for native tool calls."""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": TOOL_DESCRIPTIONS.get(name, name.replace("_", " ")),
                "parameters": _parameters(fn),
            },
        }
        for name, fn in tools_for(department).items()
    ]


def coerce_args(fn: Callable[..., Any], args: dict[str, Any]) -> dict[str, Any]:
    """Convert argument values to the types the tool declares ("3" -> 3 for an int).
    Raises ValueError with a message the model can act on."""
    hints = _hints(fn)
    out = dict(args)
    for name, value in args.items():
        want = hints.get(name)
        if want in (int, float) and isinstance(value, str):
            try:
                out[name] = want(value.strip())
            except ValueError:
                raise ValueError(f"'{name}' must be a number, got {value!r}") from None
        elif want is int and isinstance(value, float) and value.is_integer():
            out[name] = int(value)
        elif want is str and not isinstance(value, str) and value is not None:
            out[name] = str(value)
    return out


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
