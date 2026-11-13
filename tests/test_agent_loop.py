"""The agent loop with native tool calls: batches, approval-gated calls, loops, budgets, review."""
from __future__ import annotations

import json

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.llm import client as llm
from app.llm.client import Reply
from app.orchestrator import agents, nodes, progress, tools
from app.orchestrator.graph import build_graph
from app.orchestrator.multi_graph import build_team_graph

LOGISTICS = {"subject_id": "lee", "subject_roles": ["logistics"], "scratchpad": {"department": "logistics"}}
ACCOUNT = {"subject_id": "ana", "subject_roles": ["account"], "scratchpad": {"department": "account"}}


def _call(name, cid="c1", **args):
    return {"id": cid, "name": name, "args": args}


@pytest.fixture
def ran(monkeypatch):
    """Replace the tools with recorders; returns the list of calls actually executed."""
    calls = []

    def recorder(name):
        def fn(state, **kwargs):
            calls.append((name, kwargs))
            return f"{name} ok"
        return fn

    for name in ("inventory_lookup", "track_shipment", "set_reminder", "manage_bill", "balance_lookup"):
        original = tools.TOOLS[name]
        fake = recorder(name)
        fake.__signature__ = __import__("inspect").signature(original)
        fake.__annotations__ = original.__annotations__
        monkeypatch.setitem(tools.TOOLS, name, fake)
    return calls


def _script(monkeypatch, *replies):
    prompts, queue = [], list(replies)

    def _chat(messages, **kwargs):
        prompts.append((messages, kwargs))
        return queue.pop(0)

    monkeypatch.setattr(llm, "chat", _chat)
    return prompts


def _run(state, message="go"):
    graph = build_graph().compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "t"}, "recursion_limit": 50}
    return graph, config, graph.invoke({**state, "messages": [{"role": "user", "content": message}]}, config=config)


def test_several_independent_calls_run_in_one_step_and_each_gets_its_result(monkeypatch, ran):
    prompts = _script(monkeypatch, Reply("", [
        _call("inventory_lookup", "a", sku="laptop"), _call("track_shipment", "b", shipment_id="SHP-1"),
    ]), Reply("42 laptops; SHP-1 in transit."))
    _, _, final = _run(LOGISTICS)
    assert [c[0] for c in ran] == ["inventory_lookup", "track_shipment"]
    results = [m for m in final["messages"] if m["role"] == "tool"]
    assert [(m["tool_call_id"], m["name"]) for m in results] == [("a", "inventory_lookup"), ("b", "track_shipment")]
    assert final["messages"][-1]["content"] == "42 laptops; SHP-1 in transit."
    assert prompts[0][1]["tools"] and prompts[0][1].get("tool_choice") is None


def test_a_call_written_as_text_still_runs(monkeypatch, ran):
    _script(monkeypatch, Reply('call:inventory_lookup(sku="laptop")'), Reply("done"))
    _run(LOGISTICS)
    assert ran == [("inventory_lookup", {"sku": "laptop"})]


def test_numbers_given_as_text_are_converted_and_bad_ones_explained(monkeypatch):
    assert tools.coerce_args(tools.inventory_adjust, {"sku": "x", "delta": "-3"}) == {"sku": "x", "delta": -3}
    with pytest.raises(ValueError, match="delta"):
        tools.coerce_args(tools.inventory_adjust, {"sku": "x", "delta": "three"})


def test_an_approval_gated_call_never_shares_a_step(monkeypatch, ran):
    """Approving resumes the node from the top, so nothing may run before the pause."""
    _script(monkeypatch, Reply("", [
        _call("balance_lookup", "a", account="ACC-1"), _call("manage_bill", "b", bill_id="B1", detail="x"),
    ]), Reply("", [_call("manage_bill", "c", bill_id="B1", detail="x")]), Reply("Recorded."))
    graph, config, _ = _run(ACCOUNT)
    assert ran == [("balance_lookup", {"account": "ACC-1"})]  # the gated call was deferred, not run
    deferred = [m for m in graph.get_state(config).values["messages"] if m.get("tool_call_id") == "b"]
    assert "NOT RUN" in deferred[0]["content"]
    assert graph.get_state(config).next == ("tools",)  # now paused on the gated call alone

    final = graph.invoke(Command(resume={"approved": True, "reason": ""}), config=config)
    assert ran == [("balance_lookup", {"account": "ACC-1"}), ("manage_bill", {"bill_id": "B1", "detail": "x"})]
    assert final["messages"][-1]["content"] == "Recorded."


def test_repeating_the_same_call_is_refused(monkeypatch, ran):
    same = lambda cid: Reply("", [_call("inventory_lookup", cid, sku="laptop")])  # noqa: E731
    _script(monkeypatch, same("a"), same("b"), Reply("42."))
    _, _, final = _run(LOGISTICS)
    assert len(ran) == 1
    second = [m for m in final["messages"] if m.get("tool_call_id") == "b"][0]
    assert "NOT RUN" in second["content"]


def test_the_tool_budget_ends_with_an_answer_not_an_error(monkeypatch, ran):
    looping = [Reply("", [_call("inventory_lookup", f"c{i}", sku=f"sku{i}")]) for i in range(nodes.MAX_TOOL_ROUNDS)]
    prompts = _script(monkeypatch, *looping, Reply("Here is what I found."))
    _, _, final = _run(LOGISTICS)
    assert len(ran) == nodes.MAX_TOOL_ROUNDS
    messages, kwargs = prompts[-1]
    assert kwargs.get("tool_choice") == "none" and messages[-1]["content"] == nodes.BUDGET_NOTE
    assert final["messages"][-1]["content"] == "Here is what I found."


def test_an_empty_reply_is_retried_once(monkeypatch, ran):
    prompts = _script(monkeypatch, Reply(""), Reply("Answer."))
    _, _, final = _run(LOGISTICS)
    assert prompts[1][0][-1]["content"] == nodes.EMPTY_NUDGE
    assert final["messages"][-1]["content"] == "Answer."


def test_progress_steps_describe_each_call(monkeypatch, ran):
    seen = []
    progress.progress_sink.set(seen.append)
    try:
        _script(monkeypatch, Reply("", [_call("track_shipment", "a", shipment_id="SHP-8842")]), Reply("ok"))
        _run(LOGISTICS)
    finally:
        progress.progress_sink.set(None)
    assert seen == ["Tracking shipment SHP-8842"]


def test_reviewer_sends_research_back_once_with_the_gap(monkeypatch, ran):
    prompts = {"researcher": [], "reflect": 0}

    def _chat(messages, **kwargs):
        system = messages[0]["content"]
        if system.startswith("You are the PLANNER"):
            return Reply("1. find stock")
        if system.startswith("You are the RESEARCHER"):
            prompts["researcher"].append(system)
            n = len(prompts["researcher"])
            if n in (1, 3):
                return Reply("", [_call("inventory_lookup", f"r{n}", sku="laptop" if n == 1 else "monitor")])
            return Reply("I have what the plan needs.")
        if system.startswith("You are the REVIEWER"):
            prompts["reflect"] += 1
            return Reply(json.dumps({"enough": False, "missing": "monitor stock"}))
        return Reply("FINAL")

    monkeypatch.setattr(llm, "chat", _chat)
    graph = build_team_graph().compile(checkpointer=MemorySaver())
    final = graph.invoke({**LOGISTICS, "messages": [{"role": "user", "content": "stock?"}]},
                         config={"configurable": {"thread_id": "t"}, "recursion_limit": 50})
    assert prompts["reflect"] == agents.MAX_REFLECTIONS  # never more than the limit
    assert "monitor stock" in prompts["researcher"][2]  # the gap reached the researcher
    assert [c[1]["sku"] for c in ran] == ["laptop", "monitor"]
    assert final["messages"][-1]["content"] == "FINAL"


# ---- dates, continuity, honesty ----
def test_agents_are_told_the_local_date():
    import datetime as dt

    from app.orchestrator.clock import now_line

    line = now_line(dt.datetime(2026, 11, 2, 20, 0, tzinfo=dt.timezone.utc))
    assert "Tuesday, 3 November 2026" in line and "01:30" in line and "Asia/Kolkata" in line  # UTC+5:30
    assert "Today is" in nodes._system_prompt(LOGISTICS) or "[Now:" in nodes._system_prompt(LOGISTICS)
    from app.orchestrator.clock import CLOCK_MARK, DATE_RULES
    for state in (LOGISTICS, {}):
        assert nodes._system_prompt(state).endswith(DATE_RULES + CLOCK_MARK)


def test_the_time_goes_after_the_cacheable_prefix():
    """The system prompt (followed by the tools in the chat template) must not change from one
    minute to the next, or vLLM recomputes thousands of tokens for every message."""
    import datetime as dt

    from app.orchestrator.clock import CLOCK_MARK, place_clock, with_now

    def at(minute):
        msgs = [{"role": "system", "content": with_now("SYS")}, {"role": "user", "content": "old"},
                {"role": "assistant", "content": "a"}, {"role": "user", "content": "hello"},
                {"role": "tool", "content": "r", "tool_call_id": "1"}]
        return msgs, place_clock(msgs, dt.datetime(2026, 11, 2, 20, minute, tzinfo=dt.timezone.utc))

    original, one = at(0)
    _, two = at(1)
    assert one[:3] == two[:3]  # system prompt and earlier turns identical across minutes
    assert CLOCK_MARK not in one[0]["content"] and "Today is" not in one[0]["content"]
    assert one[3]["content"] == "hello\n\n[Now: Tuesday, 3 November 2026, 01:30 (Asia/Kolkata)]"
    assert two[3]["content"].endswith("[Now: Tuesday, 3 November 2026, 01:31 (Asia/Kolkata)]")
    assert one[1]["content"] == "old" and original[3]["content"] == "hello"  # input untouched


def test_the_clock_stays_in_the_system_prompt_without_a_user_turn():
    from app.orchestrator.clock import CLOCK_MARK, place_clock, with_now

    [system] = place_clock([{"role": "system", "content": with_now("SYS")}])
    assert "Today is" in system["content"] and CLOCK_MARK not in system["content"]
    assert place_clock([{"role": "user", "content": "x"}]) == [{"role": "user", "content": "x"}]


def test_only_turns_handled_as_work_carry_over_to_new_work():
    from app.api.routes_chat import _work_turns

    history = [
        {"role": "user", "content": "PERSONAL WORRY"}, {"role": "assistant", "content": "private reply"},
        {"role": "user", "content": "book a flight Delhi to Jaipur tomorrow"},
        {"role": "assistant", "content": "Sent to accounts.", "handled_by": "account"},
    ]
    turns = _work_turns(history)
    assert [t["content"] for t in turns] == ["book a flight Delhi to Jaipur tomorrow", "Sent to accounts."]
    assert "PERSONAL WORRY" not in json.dumps(turns)


def test_earlier_work_is_given_to_a_new_run_once(monkeypatch):
    from contextlib import contextmanager

    from app.acl.models import Subject
    from app.orchestrator import execution

    saver = MemorySaver()

    @contextmanager
    def _cm(mode, serde=None):
        yield build_graph().compile(checkpointer=saver)

    monkeypatch.setattr(execution, "_graph_cm", _cm)
    prompts = _script(monkeypatch, Reply("first"), Reply("second"))
    context = [{"role": "user", "content": "Delhi to Jaipur tomorrow"}, {"role": "assistant", "content": "Sent."}]
    subject = Subject("lee", ("logistics",))
    execution.run(subject, "find flights", "t", "single", "logistics", context=context)
    execution.run(subject, "economy please", "t", "single", "logistics", context=context)
    first, second = prompts[0][0], prompts[1][0]
    assert [m["content"] for m in first[1:]] == ["Delhi to Jaipur tomorrow", "Sent.", "find flights"]
    assert sum(m["content"] == "Delhi to Jaipur tomorrow" for m in second) == 1  # not added twice


def test_agents_are_told_not_to_invent_data():
    assert "Never make up" in nodes._system_prompt(LOGISTICS)
    assert "never present" in nodes._system_prompt({})
