"""Token use by task (app/llm/client.py caller_task, usage log) and its cost estimate (scripts/llm_cost.py)."""
from __future__ import annotations

import json
from types import SimpleNamespace

from app.llm import client
from scripts import llm_cost


def test_the_task_is_the_app_function_that_asked_skipping_pass_throughs():
    def chat():  # a pass-through wrapper, like scout.live_tools' chat
        return client.caller_task(depth=1)

    assert chat() == "other"  # a test module is not app code
    from app import scout

    seen = {}

    def fake(messages, **kw):
        seen["task"] = client.caller_task()
        raise RuntimeError("stop")

    try:
        scout.already_covered([{"name": "x"}], {"x": [{"title": "t", "description": ""}]},
                              lambda s, u, n: fake([]))
    except RuntimeError:
        pass
    assert seen["task"] == "scout.already_covered"


def test_usage_is_logged_as_counts_only(tmp_path, monkeypatch):
    log = tmp_path / "usage.jsonl"
    monkeypatch.setattr(client._settings, "llm_usage_log", str(log))
    usage = SimpleNamespace(prompt_tokens=1200, completion_tokens=80, prompt_tokens_details=None)
    client._record_usage("m", usage, "scout.judge")
    row = json.loads(log.read_text())
    assert row["task"] == "scout.judge" and row["in"] == 1200 and row["out"] == 80 and set(row) == {
        "t", "task", "m", "in", "out", "cached", "proc"}
    monkeypatch.setattr(client._settings, "llm_usage_log", "")
    client._record_usage("m", usage, "x")  # off: nothing written, no error
    assert len(log.read_text().splitlines()) == 1


def test_cost_estimate_uses_the_price_table():
    assert llm_cost.area("request_manager.guards.triage_verdict") == "chat: sorting each message (triage)"
    assert llm_cost.area("scout.judge") == "scouts: judging a product" and llm_cost.area("zzz.q") == "zzz.q"
    # 1M Gemma input + 100K output, nothing cached, Sonnet 5.5 (no thinking): 1.15M x $2 + 115K x $10
    assert round(llm_cost.cost("Claude Sonnet 5.5", 1e6, 1e5, 0), 2) == round(1.15 * 2 + 0.115 * 10, 2)
    # Opus thinks: output doubled
    assert round(llm_cost.cost("Claude Opus 5.5", 0, 1e6, 0), 2) == round(1.15 * 2 * 20, 2)
