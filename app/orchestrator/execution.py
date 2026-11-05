"""Run and resume orchestrator graphs on a per-user checkpoint.

Two guarantees live here:
  * Checkpoints are keyed by (subject, thread), so one user can never load
    another user's conversation by reusing their client-supplied thread_id.
  * A critical tool call pauses the graph (see nodes.run_tool). The caller gets
    a PendingAction instead of a reply, and resume() continues from the
    checkpoint once a human has decided.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langgraph.types import Command

from app.acl.models import Subject
from app.orchestrator.context import attachment_files, attachment_text, user_key
from app.orchestrator.state import TURN_COUNTERS, TURN_LISTS, TURN_RESET
from app.vault import user_keys


@dataclass(frozen=True)
class PendingAction:
    """A paused run waiting on a human decision about one exact tool call."""

    tool: str
    args: dict[str, Any]
    checkpoint_thread: str


class StaleApproval(RuntimeError):
    """The checkpoint is not paused on the tool call that was approved."""


def scoped_thread(subject_id: str, thread_id: str) -> str:
    return f"{subject_id}:{thread_id}"


def private_thread(subject_id: str, thread_id: str) -> str:
    """Checkpoint key for the private channel (/v1/chat). Separate from
    scoped_thread, so department work can never load a private conversation."""
    return f"private:{subject_id}:{thread_id}"


# LangGraph steps one run may take: 8 tool rounds is ~17 steps, a full team run ~20.
RECURSION_LIMIT = 50
# The agents graph: up to 4 hand-offs, each up to 12 tool rounds (2 steps per round).
AGENTS_RECURSION_LIMIT = 150


def builder(mode: str):
    """The uncompiled graph for a mode: single, team or agents."""
    from app.orchestrator.agents_graph import build_agents_graph
    from app.orchestrator.graph import build_graph
    from app.orchestrator.multi_graph import build_team_graph

    return {"agents": build_agents_graph, "team": build_team_graph}.get(mode, build_graph)()


def _graph_cm(mode: str, serde=None):
    # Lazy import: compiling needs a live Postgres connection.
    from app.orchestrator.agents_graph import compiled_agents_graph
    from app.orchestrator.graph import compiled_graph
    from app.orchestrator.multi_graph import compiled_team_graph

    if mode == "agents":
        return compiled_agents_graph(serde)
    return compiled_team_graph(serde) if mode == "team" else compiled_graph(serde)


def _limit(mode: str) -> int:
    return AGENTS_RECURSION_LIMIT if mode == "agents" else RECURSION_LIMIT


def _pending_interrupt(graph, config: dict) -> dict[str, Any] | None:
    for task in graph.get_state(config).tasks:
        for pending in task.interrupts:
            return pending.value
    return None


def _outcome(graph, config: dict, final: dict[str, Any]) -> str | PendingAction:
    pending = _pending_interrupt(graph, config)
    if pending is not None:
        return PendingAction(
            tool=pending["tool"],
            args=pending["args"],
            checkpoint_thread=config["configurable"]["thread_id"],
        )
    for msg in reversed(final.get("messages", [])):
        if msg.get("role") == "assistant":
            return msg.get("content", "")
    return ""


def run(
    subject: Subject,
    message: str,
    thread_id: str,
    mode: str,
    department: str | None,
    *,
    chat_key: bytes | None = None,
    attachments: str | None = None,
    context: list[dict[str, Any]] | None = None,
    files: list[dict] | None = None,
) -> str | PendingAction:
    """With a chat_key this is a private conversation: its own checkpoint namespace,
    encrypted with that user's key."""
    key = private_thread(subject.id, thread_id) if chat_key else scoped_thread(subject.id, thread_id)
    serde = user_keys.checkpoint_serde(chat_key) if chat_key else None
    # Above the agents' own budgets (8 tool rounds; 6 research steps + a review), so those
    # end a run gracefully and this only stops something truly stuck.
    config = {"configurable": {"thread_id": key}, "recursion_limit": _limit(mode)}
    initial = {
        "messages": [{"role": "user", "content": message}],
        "subject_id": subject.id,
        "subject_roles": list(subject.roles),
        "scratchpad": {"department": department, "mode": mode} if department else {"mode": mode},
        # A new message is a new request: its budgets and reports start from zero.
        **{k: 0 for k in TURN_COUNTERS},
        **{k: [TURN_RESET] for k in TURN_LISTS},
    }
    if chat_key:
        # A private run still waiting on the person's go-ahead: they moved on, so it's a no.
        waiting = private_pending(subject, thread_id, chat_key)
        if waiting is not None:
            resume_private(
                subject, thread_id, chat_key, approved=False, tool=waiting["tool"], args=waiting["args"],
                mode=waiting["mode"], reason="the person sent a new message instead of confirming",
            )
    token = attachment_text.set(attachments)
    key_token = user_key.set(chat_key)
    files_token = attachment_files.set(files)
    try:
        with _graph_cm(mode, serde) as graph:
            if context and not chat_key:
                initial["messages"] = _missing(graph, config, context) + initial["messages"]
            final = graph.invoke(initial, config=config)
            return _outcome(graph, config, final)
    finally:
        attachment_files.reset(files_token)
        user_key.reset(key_token)
        attachment_text.reset(token)


def _missing(graph, config, context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The earlier work exchanges this run doesn't already have."""
    have = {(m.get("role"), m.get("content")) for m in graph.get_state(config).values.get("messages", [])}
    return [
        {"role": m["role"], "content": m["content"]}
        for m in context
        if m.get("role") in ("user", "assistant") and (m["role"], m["content"]) not in have
    ]


# A stream says something at least this often. Cloudflare (and other proxies) close a response
# that sends nothing for ~100 s, which cut off long mail summaries mid-run (2026-11-04).
KEEPALIVE_SECONDS = 15


def private_history(subject: Subject, thread_id: str, chat_key: bytes) -> list[dict[str, Any]]:
    """The user and assistant messages of one of this user's private chats."""
    from app.orchestrator.agents import _conversation

    config = {"configurable": {"thread_id": private_thread(subject.id, thread_id)}}
    with _graph_cm("single", user_keys.checkpoint_serde(chat_key)) as graph:
        values = graph.get_state(config).values
    return [
        {"role": m["role"], "content": m["content"], "handled_by": m.get("handled_by"),
         "dashboards": m.get("dashboards") or []}
        for m in _conversation(values)
    ]


def record_private(
    subject: Subject, thread_id: str, chat_key: bytes, message: str, reply: str, handled_by: str | None,
    dashboards: list[dict] | None = None,
) -> None:
    """Add an exchange that ran elsewhere (department work) to the user's encrypted chat,
    without calling the model, so the conversation reads as one."""
    config = {"configurable": {"thread_id": private_thread(subject.id, thread_id)}}
    update = {
        "messages": [
            {"role": "user", "content": message},
            {"role": "assistant", "content": reply, "handled_by": handled_by,
             **({"dashboards": dashboards} if dashboards else {})},
        ],
        "subject_id": subject.id,
        "subject_roles": list(subject.roles),
        "scratchpad": {},
        "next": "__end__",
    }
    with _graph_cm("single", user_keys.checkpoint_serde(chat_key)) as graph:
        graph.update_state(config, update, as_node="agent")


def private_pending(subject: Subject, thread_id: str, chat_key: bytes) -> dict[str, Any] | None:
    """The action a private chat is waiting for its owner to confirm: {"tool", "args", "mode"}.
    Read from the checkpoint's own pending writes, so it doesn't matter which graph paused."""
    config = {"configurable": {"thread_id": private_thread(subject.id, thread_id)}}
    # Any graph will do: only its checkpointer is used (the same one every mode shares).
    with _graph_cm("single", user_keys.checkpoint_serde(chat_key)) as graph:
        found = graph.checkpointer.get_tuple(config)
    if found is None:
        return None
    for _, channel, value in found.pending_writes or []:
        if channel != "__interrupt__":
            continue
        for item in value if isinstance(value, (list, tuple)) else [value]:
            payload = getattr(item, "value", item)
            if isinstance(payload, dict) and "tool" in payload:
                return {"tool": payload["tool"], "args": payload.get("args", {}), "mode": payload.get("mode") or "single"}
    return None


def resume_private(
    subject: Subject, thread_id: str, chat_key: bytes, *, approved: bool, tool: str, args: dict[str, Any],
    mode: str, reason: str = "",
) -> str | PendingAction:
    """The person confirmed (or declined) an action in their own private chat."""
    config = {"configurable": {"thread_id": private_thread(subject.id, thread_id)}, "recursion_limit": _limit(mode)}
    key_token = user_key.set(chat_key)
    try:
        with _graph_cm(mode, user_keys.checkpoint_serde(chat_key)) as graph:
            pending = _pending_interrupt(graph, config)
            if pending is None or pending.get("tool") != tool or pending.get("args") != args:
                raise StaleApproval(f"chat is no longer waiting on {tool}")
            final = graph.invoke(Command(resume={"approved": approved, "reason": reason}), config=config)
            return _outcome(graph, config, final)
    finally:
        user_key.reset(key_token)


def resume(
    checkpoint_thread: str,
    mode: str,
    *,
    approved: bool,
    reason: str,
    tool: str,
    args: dict[str, Any],
) -> str | PendingAction:
    """Continue a paused run. The subject and roles come from the checkpoint."""
    config = {"configurable": {"thread_id": checkpoint_thread}, "recursion_limit": _limit(mode)}
    with _graph_cm(mode) as graph:
        pending = _pending_interrupt(graph, config)
        # An approval covers one exact call. Never resume onto anything else.
        if pending is None or pending.get("tool") != tool or pending.get("args") != args:
            raise StaleApproval(f"run is no longer paused on {tool}")
        final = graph.invoke(Command(resume={"approved": approved, "reason": reason}), config=config)
        return _outcome(graph, config, final)
