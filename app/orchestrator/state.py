"""Typed graph state shared across nodes."""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class AgentState(TypedDict, total=False):
    # PostgresSaver stores str/int/float/bool values inline as plain JSON, skipping the
    # serializer, so anything holding conversation text must be a list or dict or it
    # will not be encrypted in private chats.
    # Conversation messages. Plain additive reducer keeps entries as dicts
    # ({"role": ..., "content": ...}) rather than converting to message objects.
    messages: Annotated[list[dict[str, Any]], operator.add]
    # The acting subject (serialized: id + roles) for ACL checks inside tools.
    subject_id: str
    subject_roles: list[str]
    # Free-form scratchpad / intermediate tool results.
    scratchpad: dict[str, Any]
    # Router decision for the current step.
    next: str
    # --- multi-agent (team) fields ---
    plan: list[str]                 # planner output, one entry: the step-by-step plan
    research_notes: Annotated[list[str], operator.add]  # researcher findings
    research_steps: int             # iteration counter (bounds the research loop)
    # --- agent loop bookkeeping (counts only: no conversation text in str/int fields) ---
    tool_rounds: int                # single agent: rounds of tool calls so far (bounded)
    reflections: int                # team: times the reviewer sent research back
    review_notes: Annotated[list[str], operator.add]  # team: what the reviewer found missing
    # --- agents graph (supervisor + specialists); reports go in research_notes ---
    active_agent: str               # the specialist working now (a name, never user text)
    assignments: Annotated[list[str], operator.add]  # tasks the supervisor handed out
    handoffs: int                   # tasks handed out so far (bounded)
    agent_rounds: int               # tool rounds in the current task (bounded per specialist)
    agent_start: int                # index in messages where the current task's own work begins
    order_nudges: int               # times a shopping specialist was sent back to finish an order
