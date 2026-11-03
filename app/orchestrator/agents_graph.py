"""Compile the 'agents' graph: a supervisor handing tasks to specialist agents.

Topology (see app/orchestrator/specialists.py):
    START -> supervisor -> (research | browser | mail | booking | operations | writer)
    <specialist> -> (tools -> <specialist>)* -> supervisor
    writer -> END
"""
from __future__ import annotations

from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.orchestrator.specialists import (
    SPECIALISTS,
    make_specialist,
    specialist_tools,
    supervisor,
    writer,
)
from app.orchestrator.state import AgentState

_settings = get_settings()


def _route(state: AgentState) -> str:
    nxt = state.get("next", "__end__")
    return END if nxt == "__end__" else nxt


def build_agents_graph() -> StateGraph:
    g = StateGraph(AgentState)
    g.add_node("supervisor", supervisor)
    g.add_node("tools", specialist_tools)
    g.add_node("writer", writer)
    for name in SPECIALISTS:
        g.add_node(name, make_specialist(name))
        g.add_conditional_edges(name, _route, {"tools": "tools", "supervisor": "supervisor"})

    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", _route, {**{n: n for n in SPECIALISTS}, "writer": "writer"})
    g.add_conditional_edges("tools", _route, {**{n: n for n in SPECIALISTS}, "supervisor": "supervisor"})
    g.add_conditional_edges("writer", _route, {END: END})
    return g


@contextmanager
def compiled_agents_graph(serde=None):
    with PostgresSaver.from_conn_string(_settings.postgres_dsn) as checkpointer:
        if serde is not None:
            checkpointer.serde = serde
        yield build_agents_graph().compile(checkpointer=checkpointer)
