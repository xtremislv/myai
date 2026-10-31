"""Multi-agent 'team' graph wiring test.

Runs planner -> researcher -> research_tools -> researcher -> writer with a fake
LLM and a fake tool, so it needs neither a live model nor Postgres.
"""
from __future__ import annotations

import json

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.orchestrator import agents, tools as tools_mod
from app.orchestrator.multi_graph import build_team_graph


@pytest.fixture
def fake_llm(monkeypatch):
    """Route replies by which agent (system prompt) is calling."""
    calls = {"researcher": 0}

    def _chat(messages, **kwargs):
        system = messages[0]["content"]
        if system.startswith("You are the PLANNER"):
            return "1. Look up the doc\n2. Summarize it"
        if system.startswith("You are the RESEARCHER"):
            calls["researcher"] += 1
            if calls["researcher"] == 1:
                return json.dumps({"tool": "read_document", "args": {"key": "reports/q3.pdf"}})
            return json.dumps({"done": True})
        if system.startswith("You are the WRITER"):
            return "Q3 revenue grew 12% quarter over quarter."
        return "?"

    monkeypatch.setattr(agents.llm, "chat", _chat)
    return calls


@pytest.fixture
def fake_tool(monkeypatch):
    """Replace read_document with a stub so no DB/MinIO is touched."""
    monkeypatch.setitem(tools_mod.TOOLS, "read_document", lambda state, key: f"[contents of {key}]")


def test_team_graph_runs_planner_research_writer(fake_llm, fake_tool):
    graph = build_team_graph().compile(checkpointer=MemorySaver())
    initial = {
        "messages": [{"role": "user", "content": "Summarize the Q3 report."}],
        "subject_id": "u1",
        "subject_roles": ["researcher"],
        "scratchpad": {"department": "researcher"},
    }
    final = graph.invoke(initial, config={"configurable": {"thread_id": "t1"}})

    # Planner produced a plan.
    assert "Summarize" in final["plan"] or "doc" in final["plan"]
    # The tool ran and a note was recorded.
    assert any("reports/q3.pdf" in n for n in final["research_notes"])
    # Researcher looped exactly twice (one tool call, then done).
    assert fake_llm["researcher"] == 2
    # Writer produced the final assistant message last.
    assert final["messages"][-1]["role"] == "assistant"
    assert "Q3 revenue" in final["messages"][-1]["content"]


def test_team_graph_respects_max_steps(fake_llm, fake_tool, monkeypatch):
    """If the researcher never says done, the loop is bounded and still finishes."""
    monkeypatch.setattr(agents, "MAX_RESEARCH_STEPS", 3)
    monkeypatch.setattr(
        agents.llm,
        "chat",
        lambda messages, **k: (
            "1. plan"
            if messages[0]["content"].startswith("You are the PLANNER")
            else "final answer"
            if messages[0]["content"].startswith("You are the WRITER")
            else json.dumps({"tool": "read_document", "args": {"key": "a"}})  # never done
        ),
    )
    graph = build_team_graph().compile(checkpointer=MemorySaver())
    final = graph.invoke(
        {"messages": [{"role": "user", "content": "go"}], "subject_id": "u1",
         "subject_roles": ["researcher"], "scratchpad": {"department": "researcher"}},
        config={"configurable": {"thread_id": "t2"}},
    )
    assert final["messages"][-1]["content"] == "final answer"
    assert final["research_steps"] <= 3
