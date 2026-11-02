"""Graph nodes: router, agent (LLM), and tool executor."""
from __future__ import annotations

import inspect
import json
import logging
from typing import Any

from langgraph.types import interrupt

from app.departments.packs import critical_tools
from app.llm import client as llm
from app.observability import graph_node_total, tracer
from app.orchestrator.tools import tools_for

logger = logging.getLogger(__name__)


def _department(state: dict[str, Any]) -> str | None:
    return (state.get("scratchpad") or {}).get("department")


def _system_prompt(state: dict[str, Any]) -> str:
    dept = _department(state)
    allowed = list(tools_for(dept))
    return (
        f"You are an orchestrated agent for the '{dept or 'general'}' department on a private AI "
        "platform. You may call ONLY your department's tools to access authorized data. "
        'To call a tool, reply with ONLY a JSON object: {"tool": "<name>", "args": {...}}. '
        f"Available tools: {allowed}. "
        "When you have the final answer, reply with plain text (no JSON)."
    )


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
        reply = llm.chat(convo)

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


def _parse_tool_call(text: str) -> dict | None:
    text = text.strip()
    if not text.startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) and "tool" in obj else None
