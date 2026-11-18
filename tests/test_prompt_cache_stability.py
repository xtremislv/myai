"""Each tool round of a turn sends the previous request's text unchanged, so Claude's prompt cache
reads it back (2026-11-18: rounds after the first read only up to the person's previous message)."""
from __future__ import annotations

import json

from app.llm import anthropic_backend as ab
from app.llm.client import _for_model
from app.orchestrator import clock, nodes
from app.orchestrator.clock import CLOCK_MARK, place_clock


def _call(i, name):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": "{}"}}]}


def _result(i, n):
    return {"role": "tool", "tool_call_id": f"c{i}", "name": "open_mail", "content": f"r{i} " + "x " * n}


def _blocks(history):
    msgs = [{"role": "system", "content": "Rules." + CLOCK_MARK}, *nodes.compact_tool_results(history)]
    req = ab.params([_for_model(m) for m in place_clock(msgs)], model="claude-sonnet-5-5", max_tokens=100,
                    tools=None, tool_choice=None)
    out = [json.dumps({k: v for k, v in b.items() if k != "cache_control"}) for b in req["system"]]
    for turn in req["messages"]:
        out += [json.dumps({k: v for k, v in b.items() if k != "cache_control"}, sort_keys=True) for b in turn["content"]]
    return out


def test_each_round_extends_the_last_request_unchanged(monkeypatch):
    stamps = iter(["[Now: Wednesday, 18 November 2026, 15:42 (Asia/Kolkata)]",
                   "[Now: Wednesday, 18 November 2026, 15:43 (Asia/Kolkata)]"] * 10)
    monkeypatch.setattr(clock, "now_stamp", lambda now=None: next(stamps))
    monkeypatch.setattr(clock, "_turn_stamps", {})
    history = [{"role": "user", "content": "hi"}, _call(1, "summarize_mail"), _result(1, 5000),
               {"role": "assistant", "content": "Done."}, {"role": "user", "content": "open it"}]
    first = _blocks(history)
    history += [_call(2, "open_mail"), _result(2, 4000)]
    second = _blocks(history)
    history += [_call(3, "open_mail"), _result(3, 4000)]
    third = _blocks(history)
    assert second[:len(first)] == first and third[:len(second)] == second
    assert "r1 " + "x " * 5000 not in "".join(first)  # an earlier turn's long result is shortened...
    assert ("r2 " + "x " * 4000).strip() in "".join(third)  # ...this turn's are not, under the budget


def test_a_heavy_turn_falls_back_to_shortening_older_rounds():
    history = [{"role": "user", "content": "research"}]
    for i in range(1, 5):
        history += [_call(i, "fetch_page"), _result(i, 15_000)]
    out = nodes.compact_tool_results(history)
    shortened = [m for m in out if m.get("role") == "tool" and m["content"].endswith("[older result shortened]")]
    assert len(shortened) == 3  # all but the latest round, once the turn passes TURN_RESULT_CHARS


def test_a_new_turn_or_an_old_stamp_gets_the_time_now(monkeypatch):
    times = iter(["A", "B", "C"])
    monkeypatch.setattr(clock, "now_stamp", lambda now=None: next(times))
    monkeypatch.setattr(clock, "_turn_stamps", {})
    turn = [{"role": "user", "content": "hi"}]
    assert clock.turn_stamp(turn, 0) == "A" and clock.turn_stamp(turn, 0) == "A"
    later = [*turn, {"role": "assistant", "content": "x"}, {"role": "user", "content": "more"}]
    assert clock.turn_stamp(later, 2) == "B"
    key = clock._turn_key(turn, 0)
    clock._turn_stamps[key] = ("A", clock._turn_stamps[key][1] - clock.TURN_STAMP_SECONDS - 1)
    assert clock.turn_stamp(turn, 0) == "C"
