"""Graph nodes: router, agent (LLM), and tool executor."""
from __future__ import annotations

import ast
import inspect
import json
import logging
import re
from typing import Any

from langgraph.types import interrupt

from app.db.session import SessionLocal
from app.departments.packs import critical_tools, is_finance_call
from app.llm import client as llm
from app.observability import graph_node_total, tracer
from app.orchestrator.context import with_attachments
from app.orchestrator.tools import tool_signatures, tools_for
from app.request_manager import guards

logger = logging.getLogger(__name__)


def _department(state: dict[str, Any]) -> str | None:
    return (state.get("scratchpad") or {}).get("department")


def _system_prompt(state: dict[str, Any]) -> str:
    dept = _department(state)
    if dept is None:
        return (
            "You are a helpful AI assistant in a private conversation with one person. "
            "Answer clearly and directly. You have no access to company data or systems here."
        )
    return (
        f"You are an orchestrated agent for the '{dept}' department on a private AI "
        "platform. You may call ONLY your department's tools to access authorized data. "
        'To call a tool, reply with ONLY a JSON object: {"tool": "<name>", "args": {...}}. '
        f"Available tools, with their arguments (? means optional): {tool_signatures(dept)}. "
        "When you have the final answer, reply with plain text (no JSON)."
    )


def _notify_admin_finance(state: dict[str, Any], name: str, args: dict[str, Any]) -> None:
    """Accounts sees the admin's finance tool calls. Requests in the account department
    are noticed by the Request Manager, and critical tools when they are decided."""
    if (
        "admin" not in state.get("subject_roles", [])
        or _department(state) == "account"
        or name in critical_tools()
        or not is_finance_call(name, args)
    ):
        return
    actor = state.get("subject_id", "unknown")
    with SessionLocal() as session:
        guards.notify_accounts(session, actor, f"{actor} called {name}({json.dumps(args, default=str)})")


def run_tool(state: dict[str, Any], name: str | None, args: Any) -> Any:
    """Validate and execute one LLM-requested tool call. Failures come back as text
    so the agent can correct itself rather than crashing the run.

    Critical tools pause the graph here, before any side effect. LangGraph re-runs
    this node from the top on resume, so nothing above the interrupt may have side
    effects.
    """
    fn = tools_for(_department(state)).get(name)  # department-scoped dispatch
    if fn is None:
        return f"ERROR: tool '{name}' not available to this department"
    signature = inspect.signature(fn)
    try:
        if not isinstance(args, dict):
            raise TypeError("args must be an object")
        signature.bind(state, **args)
    except TypeError as e:
        expected = list(signature.parameters)[1:]
        return f"ERROR: invalid arguments for '{name}': {e}. Expected: {expected}"

    if name in critical_tools():
        decision = interrupt({"tool": name, "args": args, "department": _department(state)})
        if not decision.get("approved"):
            why = decision.get("reason")
            return f"DENIED: an approver rejected {name}" + (f": {why}" if why else "")

    try:
        _notify_admin_finance(state, name, args)  # inside the try: no notice, no tool call
        return fn(state, **args)
    except Exception:  # noqa: BLE001 - one failing tool must not kill the run
        logger.exception("tool %s failed", name)
        return f"ERROR: tool '{name}' failed"


def router(state: dict[str, Any]) -> dict[str, Any]:
    """Decide whether to run the agent or finish. Entry point normalizes state."""
    graph_node_total.labels(node="router").inc()
    msgs = state.get("messages", [])
    # If the last message is from the user or a tool, let the agent act.
    if msgs and msgs[-1].get("role") in ("user", "tool"):
        return {"next": "agent"}
    return {"next": "__end__"}


def agent(state: dict[str, Any]) -> dict[str, Any]:
    """Call the LLM. If it emits a tool-call JSON, route to tools; else finish."""
    graph_node_total.labels(node="agent").inc()
    with tracer.start_as_current_span("graph.agent"):
        convo = [{"role": "system", "content": _system_prompt(state)}, *state.get("messages", [])]
        reply = llm.chat(with_attachments(convo), stream=True)

    tool_call = _parse_tool_call(reply)
    if tool_call:
        return {"messages": [{"role": "assistant", "content": reply}], "next": "tools"}
    return {"messages": [{"role": "assistant", "content": reply}], "next": "__end__"}


def tools_node(state: dict[str, Any]) -> dict[str, Any]:
    """Execute the tool requested in the last assistant message."""
    graph_node_total.labels(node="tools").inc()
    last = state["messages"][-1]["content"]
    call = _parse_tool_call(last) or {}
    name, args = call.get("tool"), call.get("args", {})
    with tracer.start_as_current_span("graph.tool") as span:
        span.set_attribute("tool.name", name or "unknown")
        result = run_tool(state, name, args)
    return {
        "messages": [{"role": "tool", "content": json.dumps(result, default=str)}],
        "next": "agent",
    }


_NATIVE_CALL = re.compile(r"call:([A-Za-z_][A-Za-z0-9_]*)\((.*)\)", re.DOTALL)


def _parse_native_call(text: str) -> dict | None:
    """Gemma 4 sometimes ignores the JSON instruction and answers in its own syntax,
    call:name(key="value", n=3). Only keyword arguments with literal values are accepted:
    the arguments are parsed with ast and literal_eval, never executed."""
    match = _NATIVE_CALL.fullmatch(text)
    if not match:
        return None
    name, inner = match.groups()
    try:
        call = ast.parse(f"f({inner})", mode="eval").body
        if not isinstance(call, ast.Call) or call.args:
            return None
        args = {}
        for kw in call.keywords:
            if kw.arg is None:  # **kwargs
                return None
            args[kw.arg] = ast.literal_eval(kw.value)
    except (SyntaxError, ValueError):
        return None
    return {"tool": name, "args": args}


def _parse_tool_call(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("call:"):
        return _parse_native_call(text)
    if not text.startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) and "tool" in obj else None
