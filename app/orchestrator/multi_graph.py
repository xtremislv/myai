"""Compile the multi-agent 'team' graph with a Postgres checkpointer.

Topology:
    START -> planner -> researcher -> (research_tools -> researcher)* -> reflect
          reflect -> researcher (once, if something is missing) | writer -> END
"""
from __future__ import annotations

from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.orchestrator.agents import planner, reflect, research_tools, researcher, writer
from app.orchestrator.state import AgentState

_settings = get_settings()


def _route(state: AgentState) -> str:
    nxt = state.get("next", "__end__")
    return END if nxt == "__end__" else nxt


def build_team_graph() -> StateGraph:
    g = StateGraph(AgentState)
    g.add_node("planner", planner)
    g.add_node("researcher", researcher)
    g.add_node("research_tools", research_tools)
    g.add_node("reflect", reflect)
    g.add_node("writer", writer)

    g.add_edge(START, "planner")
    g.add_edge("planner", "researcher")
    g.add_conditional_edges(
        "researcher", _route, {"tools": "research_tools", "reflect": "reflect", "writer": "writer", END: END}
    )
    g.add_edge("research_tools", "researcher")
    g.add_conditional_edges("reflect", _route, {"researcher": "researcher", "writer": "writer"})
    g.add_conditional_edges("writer", _route, {END: END})
    return g


@contextmanager
def compiled_team_graph(serde=None):
    with PostgresSaver.from_conn_string(_settings.postgres_dsn) as checkpointer:
        if serde is not None:
            checkpointer.serde = serde
        yield build_team_graph().compile(checkpointer=checkpointer)
