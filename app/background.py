"""Any long task, run by the agent in the background, with the result saved as a sheet or a document.

A chat turn has a few tool rounds and the person watching. Some requests need far more: read dozens
of pages, compare many products, go through a long list of sites. ``run`` gives the agent its own
loop for those, in a background job (app/jobs.py): many more rounds, no one to ask, and at the end
the result is written as a Word document, an Excel sheet, or both, saved in the person's files (in
private chat) or the department's folder (at work). The person gets the bell notification and
pop-up, and an email and a reminder if they asked.

It runs with the same tools, scoping and ACL as the chat that started it, minus anything that needs
a person present (approvals, confirmations, the live browser, saved payment details) and anything
that would start another background job.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

MAX_ROUNDS = 30            # tool rounds (a chat turn has 8)
MAX_CALLS_PER_ROUND = 6
HISTORY_CHARS = 120_000    # older tool results shrink further past this (64k-token context)
OUTPUTS = ("doc", "sheet", "both")

# Need a person present, or would start another job.
NOT_IN_BACKGROUND = frozenset({
    "ask_person_to_browse", "ask_person_for_secret", "list_saved_details", "fill_saved",
    "browser_open", "browser_read", "browser_click", "browser_type", "browser_select", "browser_scroll",
    "browser_back", "browser_commit", "send_mail", "delete_file", "initiate_payment", "manage_bill",
    "manage_order", "book_service", "mail_to_sheet", "start_background_task", "web_research", "job_status",
    "make_dashboard",
})

BACKGROUND_NOTE = (
    "\n\nYOU ARE RUNNING AS A BACKGROUND TASK. Nobody is watching and nobody can answer questions, so "
    "never ask any: make sensible choices and note them. You have up to {rounds} rounds of tool calls (several "
    "calls per round, run together), far more than a chat: be thorough. Cover many sources, open the "
    "pages and threads themselves rather than trusting snippets, and keep going until the task is "
    "really done. Actions that need a person (sending mail, deleting, buying, approvals) are not "
    "available here: describe them in the result instead. When you have finished, stop calling tools "
    "and write the complete result."
)
FINAL_DOC = (
    "Now write the complete result for the person as a well-organised Markdown document: a short "
    "summary first, then the details (headings, bullet points, tables where useful), and the sources "
    "(links) you used. Use only what you found; say what you couldn't check."
)
FINAL_ROWS = (
    "Now give the result as a table. Reply with ONLY a JSON array of objects, one per row, {cols} "
    "Include every item you found (not just a few), with a Source column holding the link it came "
    "from. Values are short text or numbers; \"\" when unknown."
)


@dataclass
class Outcome:
    markdown: str
    rows: list[dict]
    columns: list[str]
    rounds: int
    calls: int


def _shrink(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the conversation inside the context: the oldest tool results go down to a stub first."""
    total = sum(len(str(m.get("content") or "")) for m in messages)
    out = list(messages)
    for i, m in enumerate(out):
        if total <= HISTORY_CHARS:
            break
        if m.get("role") == "tool" and len(str(m.get("content") or "")) > 400:
            text = str(m["content"])
            total -= len(text) - 400
            out[i] = {**m, "content": text[:400] + " [... older result shortened]"}
    return out


def run(state: dict[str, Any], task: str, output: str, columns: list[str], progress: Callable[[str], None],
        chat: Callable | None = None, rounds: int = MAX_ROUNDS) -> Outcome:
    """The agent loop without a person: tools until done, then the result as Markdown and/or rows."""
    from app import bigsummary
    from app.departments.packs import critical_tools
    from app.llm import client as llm
    from app.orchestrator import nodes
    from app.orchestrator.progress import tool_step
    from app.orchestrator.tools import tool_schemas

    chat = chat or llm.chat
    blocked = NOT_IN_BACKGROUND | critical_tools()
    tools = [t for t in tool_schemas(nodes._department(state)) if t["function"]["name"] not in blocked]
    system = nodes._system_prompt(state) + BACKGROUND_NOTE.format(rounds=rounds)
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    calls_made, used = 0, 0
    for used in range(1, rounds + 1):
        convo = [{"role": "system", "content": system}, *_shrink(nodes.compact_tool_results(messages))]
        reply = chat(convo, tools=tools)
        # A tool missing from the schemas can still be named; renamed, run_tool answers "not available"
        # (a critical tool would otherwise reach interrupt(), which only works inside the graph).
        calls = [c if c["name"] not in blocked else {**c, "name": f"{c['name']}_not_available_in_background"}
                 for c in nodes.calls_in(reply)[:MAX_CALLS_PER_ROUND]]
        if not calls:
            break
        progress(f"Step {used}: " + "; ".join(tool_step(c["name"], c["args"]) for c in calls)[:200])
        messages.append(nodes.assistant_calls(reply, calls))
        results, _ = nodes.execute_calls({**state, "messages": messages}, calls)
        messages.extend(results)
        calls_made += len(calls)
    progress("Writing the result")
    convo = [{"role": "system", "content": system}, *_shrink(nodes.compact_tool_results(messages))]
    markdown, rows = "", []
    if output in ("doc", "both"):
        markdown = str(chat([*convo, {"role": "user", "content": FINAL_DOC}], tools=tools, tool_choice="none",
                            max_tokens=bigsummary.FINAL_TOKENS)).strip()
    if output in ("sheet", "both"):
        cols = (f"with exactly these keys: {json.dumps(columns + (['Source'] if 'Source' not in columns else []))}."
                if columns else "with clear, consistent keys (at most 10) and a Source key.")
        reply = str(chat([*convo, {"role": "user", "content": FINAL_ROWS.format(cols=cols)}], tools=tools,
                         tool_choice="none", max_tokens=bigsummary.FINAL_TOKENS))
        rows = bigsummary._parse_rows(reply)
        if not columns:
            for r in rows:
                columns += [k for k in r if k not in columns]
        elif "Source" not in columns:
            columns = [*columns, "Source"]
        rows = bigsummary._clean_rows(rows, columns)
    return Outcome(markdown, rows, columns, used, calls_made)
