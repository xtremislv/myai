"""Typed graph state shared across nodes."""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

# Lists that belong to one request (reports, tasks, review notes) start empty on every new message:
# the run's first update for them is [TURN_RESET]. With plain operator.add they grew for the whole
# thread, so a long thread reached its handoff budget and the writer kept repeating an old report
# without any agent working again (seen 2026-11-05: "I cannot provide ..." three times running).
TURN_RESET = "\x00new-turn"


def per_turn(old: list | None, new: list | None) -> list:
    new = list(new or [])
    if new and new[0] == TURN_RESET:
        return new[1:]
    return list(old or []) + new


# Counters that are per request; execution.run sets them to 0 for every new message.
TURN_COUNTERS = ("tool_rounds", "research_steps", "reflections", "handoffs", "agent_rounds", "agent_start", "order_nudges")
TURN_LISTS = ("research_notes", "review_notes", "assignments")


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
    research_notes: Annotated[list[str], per_turn]  # researcher findings (this request)
    research_steps: int             # iteration counter (bounds the research loop)
    # --- agent loop bookkeeping (counts only: no conversation text in str/int fields) ---
    tool_rounds: int                # single agent: rounds of tool calls so far (bounded)
    reflections: int                # team: times the reviewer sent research back
    review_notes: Annotated[list[str], per_turn]  # team: what the reviewer found missing
    # --- agents graph (supervisor + specialists); reports go in research_notes ---
    active_agent: str               # the specialist working now (a name, never user text)
    assignments: Annotated[list[str], per_turn]  # tasks the supervisor handed out (this request)
    handoffs: int                   # tasks handed out so far (bounded)
    agent_rounds: int               # tool rounds in the current task (bounded per specialist)
    agent_start: int                # index in messages where the current task's own work begins
    order_nudges: int               # times a shopping specialist was sent back to finish an order
