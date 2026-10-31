"""Typed graph state shared across nodes."""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class AgentState(TypedDict, total=False):
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
    plan: str                       # planner output: the step-by-step plan
    research_notes: Annotated[list[str], operator.add]  # researcher findings
    research_steps: int             # iteration counter (bounds the research loop)
