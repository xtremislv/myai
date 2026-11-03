"""Specialized agents for the multi-agent 'team' graph.

Flow:  planner -> researcher -> (tools -> researcher)* -> reflect -> (researcher ...)? -> writer

* planner    turns the request into a short, ordered plan.
* researcher follows the plan with the department's tools (native function calls, several per
             step), recording each result as a note. It stops when it answers without calling
             a tool, or after MAX_RESEARCH_STEPS rounds.
* reflect    checks the notes against the request and plan. If something the answer needs is
             missing and there is budget left, it names the gap and sends the researcher back
             (at most MAX_REFLECTIONS times). This happens before writing, so the person only
             ever sees one answer.
* writer     composes the final answer from the plan, notes and conversation, streaming it.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.llm import client as llm
from app.observability import graph_node_total, tracer
from app.orchestrator.clock import with_now
from app.orchestrator.context import with_attachments
from app.orchestrator.nodes import (
    _department,
    _parse_tool_call,
    assistant_calls,
    calls_in,
    execute_calls,
    pending_calls,
)
from app.orchestrator.progress import step
from app.orchestrator.tools import tool_schemas

MAX_RESEARCH_STEPS = 6
MAX_REFLECTIONS = 1

PLANNER_PROMPT = (
    "You are the PLANNER in a team of agents. Read the user's request and produce a "
    "short, numbered plan (max 5 steps) describing what information to gather and how "
    "to answer. Reply with the plan only — no preamble."
)


def _researcher_prompt(state: dict) -> str:
    gaps = state.get("review_notes") or []
    gap_text = (
        " A reviewer checked your notes and found this still missing; gather it now: " + gaps[-1]
        if gaps else ""
    )
    return (
        "You are the RESEARCHER. Follow the plan and gather facts using your department's tools. "
        "Call several tools at once when they don't depend on each other. If a tool returns an "
        "ERROR, fix the arguments and try again. When you have everything the plan needs, reply "
        "with one short sentence saying so, without calling a tool." + gap_text
    )


REFLECT_PROMPT = (
    "You are the REVIEWER in a team of agents. Compare the research notes with the user's "
    "request and the plan. Decide whether the notes contain enough to answer the request well. "
    "Results marked [stub] or ERROR count as gathered if retrying would not help. "
    'Reply with ONLY JSON: {"enough": true} or {"enough": false, "missing": "<the specific '
    'fact still needed, in one sentence>"}'
)

WRITER_PROMPT = (
    "You are the WRITER. Using the plan and the research notes, write the final answer "
    "for the user. Be clear and concise. Do not mention the internal plan or tools. If some "
    "data came from a system marked [stub], say that system isn't connected yet. State only what "
    "the notes and conversation support: never make up schedules, prices, stock or records."
)


def planner(state: dict[str, Any]) -> dict[str, Any]:
    graph_node_total.labels(node="planner").inc()
    step("Planning")
    with tracer.start_as_current_span("team.planner"):
        convo = [{"role": "system", "content": with_now(PLANNER_PROMPT)}, *_conversation(state)]
        plan = llm.chat(with_attachments(convo))
    return {
        "plan": [plan],
        "research_steps": 0,
        "reflections": 0,
        "messages": [{"role": "assistant", "content": f"[plan]\n{plan}"}],
        "next": "researcher",
    }


def researcher(state: dict[str, Any]) -> dict[str, Any]:
    graph_node_total.labels(node="researcher").inc()
    steps = state.get("research_steps", 0)
    tools = tool_schemas(_department(state))
    if steps >= MAX_RESEARCH_STEPS or not tools:
        return {"next": "reflect"}

    with tracer.start_as_current_span("team.researcher"):
        convo = [
            {"role": "system", "content": with_now(_researcher_prompt(state))},
            {"role": "assistant", "content": f"[plan]\n{_plan(state)}"},
            *_notes_as_messages(state),
            *_conversation(state),
        ]
        reply = llm.chat(with_attachments(convo), tools=tools)

    calls = calls_in(reply)
    if calls:
        return {"messages": [assistant_calls(reply, calls)], "research_steps": steps + 1, "next": "tools"}
    return {"research_steps": steps + 1, "next": "reflect"}


def research_tools(state: dict[str, Any]) -> dict[str, Any]:
    """Run the researcher's calls; each result becomes a note."""
    graph_node_total.labels(node="research_tools").inc()
    messages, notes = execute_calls(state, pending_calls(state))
    return {"research_notes": notes, "messages": messages, "next": "researcher"}


def reflect(state: dict[str, Any]) -> dict[str, Any]:
    """Send the researcher back once if the notes can't answer the request yet."""
    graph_node_total.labels(node="reflect").inc()
    can_retry = (
        state.get("reflections", 0) < MAX_REFLECTIONS
        and state.get("research_steps", 0) < MAX_RESEARCH_STEPS
        and bool(tool_schemas(_department(state)))
    )
    if not can_retry:
        return {"next": "writer"}

    step("Checking the findings")
    with tracer.start_as_current_span("team.reflect"):
        convo = [
            {"role": "system", "content": with_now(REFLECT_PROMPT)},
            {"role": "assistant", "content": f"[plan]\n{_plan(state)}"},
            *_notes_as_messages(state),
            *_conversation(state),
        ]
        verdict = _verdict(llm.chat(with_attachments(convo), temperature=0, max_tokens=120))
    if verdict is None or verdict.get("enough", True) or not verdict.get("missing"):
        return {"next": "writer"}
    step("Looking for what's still missing")
    return {
        "review_notes": [str(verdict["missing"])[:300]],
        "reflections": state.get("reflections", 0) + 1,
        "next": "researcher",
    }


def writer(state: dict[str, Any]) -> dict[str, Any]:
    graph_node_total.labels(node="writer").inc()
    step("Writing the answer")
    with tracer.start_as_current_span("team.writer"):
        convo = [
            {"role": "system", "content": with_now(WRITER_PROMPT)},
            {"role": "assistant", "content": f"[plan]\n{_plan(state)}"},
            *_notes_as_messages(state),
            *_conversation(state),
        ]
        answer = llm.chat(with_attachments(convo), stream=True)  # the only part users see
    return {"messages": [{"role": "assistant", "content": answer}], "next": "__end__"}


def _verdict(reply: str) -> dict | None:
    match = re.search(r"\{.*\}", reply, re.DOTALL)
    if match is None:
        return None
    try:
        verdict = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    return verdict if isinstance(verdict, dict) else None


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
            and not m.get("tool_calls")
            and not (m.get("content") or "").startswith("[plan]")
            and _parse_tool_call(m.get("content") or "") is None
        )
    ]


def _notes_as_messages(state: dict[str, Any]) -> list[dict[str, str]]:
    notes = state.get("research_notes", [])
    if not notes:
        return []
    joined = "\n".join(f"- {n}" for n in notes)
    return [{"role": "assistant", "content": f"[research notes]\n{joined}"}]
