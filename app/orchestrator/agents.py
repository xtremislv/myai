"""Specialized agents for the multi-agent 'team' graph.

Flow:  planner  ->  researcher  ->  (tools -> researcher)*  ->  writer

* planner    turns the user request into a short, ordered plan.
* researcher executes the plan, calling ACL-gated tools to gather facts, and
             records findings into state['research_notes']. It loops with the
             tool executor until it has enough (or hits MAX_RESEARCH_STEPS).
* writer     composes the final answer from the plan + notes + conversation.
"""
from __future__ import annotations

import json
from typing import Any

from app.llm import client as llm
from app.observability import graph_node_total, tracer
from app.orchestrator.nodes import _parse_tool_call
from app.orchestrator.tools import TOOLS

MAX_RESEARCH_STEPS = 6

PLANNER_PROMPT = (
    "You are the PLANNER in a team of agents. Read the user's request and produce a "
    "short, numbered plan (max 5 steps) describing what information to gather and how "
    "to answer. Reply with the plan only — no preamble."
)

RESEARCHER_PROMPT = (
    "You are the RESEARCHER. Follow the plan and gather facts using tools. "
    "To call a tool, reply with ONLY a JSON object: "
    '{"tool": "<name>", "args": {...}}. '
    f"Available tools: {list(TOOLS)}. "
    "When you have gathered everything the plan needs, reply with ONLY the JSON "
    '{"done": true} and nothing else.'
)

WRITER_PROMPT = (
    "You are the WRITER. Using the plan and the research notes, write the final answer "
    "for the user. Be clear and concise. Do not mention the internal plan or tools."
)


def planner(state: dict[str, Any]) -> dict[str, Any]:
    graph_node_total.labels(node="planner").inc()
    with tracer.start_as_current_span("team.planner"):
        convo = [{"role": "system", "content": PLANNER_PROMPT}, *state.get("messages", [])]
        plan = llm.chat(convo)
    return {
        "plan": plan,
        "research_steps": 0,
        "messages": [{"role": "assistant", "content": f"[plan]\n{plan}"}],
        "next": "researcher",
    }


def researcher(state: dict[str, Any]) -> dict[str, Any]:
    graph_node_total.labels(node="researcher").inc()
    steps = state.get("research_steps", 0)
    if steps >= MAX_RESEARCH_STEPS:
        return {"next": "writer"}

    with tracer.start_as_current_span("team.researcher"):
        convo = [
            {"role": "system", "content": RESEARCHER_PROMPT},
            {"role": "assistant", "content": f"[plan]\n{state.get('plan', '')}"},
            *_notes_as_messages(state),
            *state.get("messages", [])[:1],  # the original user request
        ]
        reply = llm.chat(convo)

    call = _parse_tool_call(reply)
    if call and "tool" in call:
        # Hand the tool call to the executor, then come back to researcher.
        return {
            "messages": [{"role": "assistant", "content": reply}],
            "research_steps": steps + 1,
            "next": "tools",
        }
    # {"done": true} or any non-tool reply -> move on to writing.
    return {"research_steps": steps + 1, "next": "writer"}


def research_tools(state: dict[str, Any]) -> dict[str, Any]:
    """Execute the researcher's tool call and store the result as a note."""
    graph_node_total.labels(node="research_tools").inc()
    last = state["messages"][-1]["content"]
    call = _parse_tool_call(last) or {}
    name, args = call.get("tool"), call.get("args", {})
    fn = TOOLS.get(name)
    with tracer.start_as_current_span("team.tool") as span:
        span.set_attribute("tool.name", name or "unknown")
        result = f"ERROR: unknown tool '{name}'" if fn is None else fn(state, **args)
    note = f"{name}({json.dumps(args, default=str)}) -> {json.dumps(result, default=str)}"
    return {
        "research_notes": [note],
        "messages": [{"role": "tool", "content": json.dumps(result, default=str)}],
        "next": "researcher",
    }


def writer(state: dict[str, Any]) -> dict[str, Any]:
    graph_node_total.labels(node="writer").inc()
    with tracer.start_as_current_span("team.writer"):
        convo = [
            {"role": "system", "content": WRITER_PROMPT},
            {"role": "assistant", "content": f"[plan]\n{state.get('plan', '')}"},
            *_notes_as_messages(state),
            *state.get("messages", [])[:1],
        ]
        answer = llm.chat(convo)
    return {"messages": [{"role": "assistant", "content": answer}], "next": "__end__"}


def _notes_as_messages(state: dict[str, Any]) -> list[dict[str, str]]:
    notes = state.get("research_notes", [])
    if not notes:
        return []
    joined = "\n".join(f"- {n}" for n in notes)
    return [{"role": "assistant", "content": f"[research notes]\n{joined}"}]
