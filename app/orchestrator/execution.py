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
from app.orchestrator.context import attachment_text
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


def _graph_cm(mode: str, serde=None):
    # Lazy import: compiling needs a live Postgres connection.
    from app.orchestrator.graph import compiled_graph
    from app.orchestrator.multi_graph import compiled_team_graph

    return compiled_team_graph(serde) if mode == "team" else compiled_graph(serde)


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
) -> str | PendingAction:
    """With a chat_key this is a private conversation: its own checkpoint namespace,
    encrypted with that user's key."""
    key = private_thread(subject.id, thread_id) if chat_key else scoped_thread(subject.id, thread_id)
    serde = user_keys.checkpoint_serde(chat_key) if chat_key else None
    config = {"configurable": {"thread_id": key}}
    initial = {
        "messages": [{"role": "user", "content": message}],
        "subject_id": subject.id,
        "subject_roles": list(subject.roles),
        "scratchpad": {"department": department} if department else {},
    }
    token = attachment_text.set(attachments)
    try:
        with _graph_cm(mode, serde) as graph:
            final = graph.invoke(initial, config=config)
            return _outcome(graph, config, final)
    finally:
        attachment_text.reset(token)


def private_history(subject: Subject, thread_id: str, chat_key: bytes) -> list[dict[str, Any]]:
    """The user and assistant messages of one of this user's private chats."""
    from app.orchestrator.agents import _conversation

    config = {"configurable": {"thread_id": private_thread(subject.id, thread_id)}}
    with _graph_cm("single", user_keys.checkpoint_serde(chat_key)) as graph:
        values = graph.get_state(config).values
    return [
        {"role": m["role"], "content": m["content"], "handled_by": m.get("handled_by")}
        for m in _conversation(values)
    ]


def record_private(
    subject: Subject, thread_id: str, chat_key: bytes, message: str, reply: str, handled_by: str | None
) -> None:
    """Add an exchange that ran elsewhere (department work) to the user's encrypted chat,
    without calling the model, so the conversation reads as one."""
    config = {"configurable": {"thread_id": private_thread(subject.id, thread_id)}}
    update = {
        "messages": [
            {"role": "user", "content": message},
            {"role": "assistant", "content": reply, "handled_by": handled_by},
        ],
        "subject_id": subject.id,
        "subject_roles": list(subject.roles),
        "scratchpad": {},
        "next": "__end__",
    }
    with _graph_cm("single", user_keys.checkpoint_serde(chat_key)) as graph:
        graph.update_state(config, update, as_node="agent")


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
    config = {"configurable": {"thread_id": checkpoint_thread}}
    with _graph_cm(mode) as graph:
        pending = _pending_interrupt(graph, config)
        # An approval covers one exact call. Never resume onto anything else.
        if pending is None or pending.get("tool") != tool or pending.get("args") != args:
            raise StaleApproval(f"run is no longer paused on {tool}")
        final = graph.invoke(Command(resume={"approved": approved, "reason": reason}), config=config)
        return _outcome(graph, config, final)
