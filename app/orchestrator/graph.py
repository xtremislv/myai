"""Compile the LangGraph state machine with a Postgres checkpointer.

Topology:
    START -> router -> (agent | END)
    agent -> (tools | END)      # agent decides via state["next"]
    tools -> agent              # loop until agent produces a final answer
"""
from __future__ import annotations

from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.orchestrator.nodes import agent, router, tools_node
from app.orchestrator.state import AgentState

_settings = get_settings()


def _route(state: AgentState) -> str:
    nxt = state.get("next", "__end__")
    return END if nxt == "__end__" else nxt


def build_graph() -> StateGraph:
    g = StateGraph(AgentState)
    g.add_node("router", router)
    g.add_node("agent", agent)
    g.add_node("tools", tools_node)

    g.add_edge(START, "router")
    g.add_conditional_edges("router", _route, {"agent": "agent", END: END})
    g.add_conditional_edges("agent", _route, {"tools": "tools", END: END})
    g.add_edge("tools", "agent")
    return g


@contextmanager
def compiled_graph():
    """Yield a graph compiled with a live Postgres checkpointer connection."""
    with PostgresSaver.from_conn_string(_settings.postgres_dsn) as checkpointer:
        checkpointer.setup()  # idempotent: creates checkpoint tables if missing
        yield build_graph().compile(checkpointer=checkpointer)
