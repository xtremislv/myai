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
from app.orchestrator.nodes import _department, _parse_tool_call, run_tool
from app.orchestrator.tools import tools_for

MAX_RESEARCH_STEPS = 6

PLANNER_PROMPT = (
    "You are the PLANNER in a team of agents. Read the user's request and produce a "
    "short, numbered plan (max 5 steps) describing what information to gather and how "
    "to answer. Reply with the plan only — no preamble."
)

def _researcher_prompt(state: dict) -> str:
    allowed = list(tools_for(_department(state)))
    return (
        "You are the RESEARCHER. Follow the plan and gather facts using ONLY your department's tools. "
        'To call a tool, reply with ONLY a JSON object: {"tool": "<name>", "args": {...}}. '
        f"Available tools: {allowed}. "
        'When you have gathered everything the plan needs, reply with ONLY the JSON {"done": true}.'
    )

WRITER_PROMPT = (
    "You are the WRITER. Using the plan and the research notes, write the final answer "
    "for the user. Be clear and concise. Do not mention the internal plan or tools."
)


def planner(state: dict[str, Any]) -> dict[str, Any]:
    graph_node_total.labels(node="planner").inc()
    with tracer.start_as_current_span("team.planner"):
        convo = [{"role": "system", "content": PLANNER_PROMPT}, *_conversation(state)]
        plan = llm.chat(convo)
    return {
        "plan": [plan],
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
            {"role": "system", "content": _researcher_prompt(state)},
            {"role": "assistant", "content": f"[plan]\n{_plan(state)}"},
            *_notes_as_messages(state),
            *_conversation(state),
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
    with tracer.start_as_current_span("team.tool") as span:
        span.set_attribute("tool.name", name or "unknown")
        result = run_tool(state, name, args)
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
            {"role": "assistant", "content": f"[plan]\n{_plan(state)}"},
            *_notes_as_messages(state),
            *_conversation(state),
        ]
        answer = llm.chat(convo)
    return {"messages": [{"role": "assistant", "content": answer}], "next": "__end__"}


def _plan(state: dict[str, Any]) -> str:
    plan = state.get("plan") or [""]
    return plan[0]


def _conversation(state: dict[str, Any]) -> list[dict[str, Any]]:
    """What the user and the team said to each other, ending with the current
    request. Excludes internal plans, tool calls and tool results."""
    return [
        m
        for m in state.get("messages", [])
        if m.get("role") == "user"
        or (
            m.get("role") == "assistant"
            and not m.get("content", "").startswith("[plan]")
            and _parse_tool_call(m.get("content", "")) is None
        )
    ]


def _notes_as_messages(state: dict[str, Any]) -> list[dict[str, str]]:
    notes = state.get("research_notes", [])
    if not notes:
        return []
    joined = "\n".join(f"- {n}" for n in notes)
    return [{"role": "assistant", "content": f"[research notes]\n{joined}"}]
