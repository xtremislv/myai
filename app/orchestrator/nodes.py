"""Graph nodes: router, agent (LLM), and tool executor."""
from __future__ import annotations

import json
from typing import Any

from app.llm import client as llm
from app.observability import graph_node_total, tracer
from app.orchestrator.tools import TOOLS

SYSTEM_PROMPT = (
    "You are an orchestrated agent running on a private AI platform. "
    "You may call tools to access the user's authorized documents and secrets. "
    "To call a tool, reply with ONLY a JSON object: "
    '{"tool": "<name>", "args": {...}}. '
    f"Available tools: {list(TOOLS)}. "
    "When you have the final answer, reply with plain text (no JSON)."
)


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
        convo = [{"role": "system", "content": SYSTEM_PROMPT}, *state.get("messages", [])]
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
    fn = TOOLS.get(name)
    with tracer.start_as_current_span("graph.tool") as span:
        span.set_attribute("tool.name", name or "unknown")
        if fn is None:
            result = f"ERROR: unknown tool '{name}'"
        else:
            result = fn(state, **args)
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
