"""The model manager (app/llm/model_manager.py): tier per call, monthly mix, budget, alerts."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from app.llm import model_manager as mm


@pytest.fixture
def fresh(monkeypatch):
    s = mm.get_settings()
    for key, value in {"llm_provider": "anthropic", "llm_model": "claude-sonnet-5-5", "llm_standard_model": "",
                       "llm_smart_routing": True, "llm_monthly_budget": 50000, "llm_budget_currency": "INR",
                       "llm_usd_rate": 88.0, "llm_budget_hard_stop": False,
                       "llm_tier_targets": "light=50,standard=35,complex=10,deep=5"}.items():
        monkeypatch.setattr(s, key, value)
    ledger = mm.Ledger(redis_client=False)
    monkeypatch.setattr(mm, "ledger", ledger)
    return ledger


def user(text):
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": text}]


def test_base_tiers_follow_the_work(fresh):
    assert mm.choose("request_manager.guards.triage", user("think deeply about this")).model == "claude-haiku-5-5"
    assert mm.choose("scout.candidates", user("x")).tier == "light"
    assert mm.choose("orchestrator.nodes.agent", user("what's the weather")).model == "claude-sonnet-5-5"
    assert mm.choose("orchestrator.nodes.agent", user("Compare these two phones")).model == "claude-opus-5-5"
    assert mm.choose("orchestrator.specialists.writer", user("summarise")).tier == "complex"
    deep = mm.choose("orchestrator.nodes.agent", user("Think deeply: should we launch a second channel?"))
    assert deep.model == "claude-fable-5-2" and "deep" in deep.reason
    tools = [{"role": "tool", "content": "r"}] * mm.COMPLEX_TOOL_ROUNDS
    assert mm.choose("orchestrator.nodes.agent", [*user("find it"), *tools]).tier == "complex"
    assert mm.choose("scout.judge", user("x"), background=True).tier == "standard"
    assert mm.choose("background.run", user("x"), background=True).tier == "standard"  # an answer task
    assert mm.choose("other.thing", user("x"), background=True).tier == "light"
    assert mm.choose("x", user("y"), requested="claude-opus-5-5").tier == "complex"


def test_tiers_over_their_share_step_down(fresh):
    month = mm.month_key()
    for tier, n in {"light": 50, "standard": 30, "complex": 5, "deep": 15}.items():  # deep at 15% > 5%
        fresh.add(mm._k(month, "calls", tier), n)
    c = mm.choose("orchestrator.nodes.agent", user("think deeply about our plan"))
    assert c.tier == "complex" and "deep over its 5% share" in c.reason
    for _ in range(40):  # standard now well over 35%: a plain answer steps down to light
        fresh.add(mm._k(month, "calls", "standard"), 1)
    assert mm.choose("orchestrator.nodes.agent", user("hello")).tier == "light"


def test_light_under_its_share_takes_background_standard_work(fresh):
    month = mm.month_key()
    fresh.add(mm._k(month, "calls", "light"), 20)
    fresh.add(mm._k(month, "calls", "standard"), 30)
    fresh.add(mm._k(month, "calls", "complex"), 10)
    fresh.add(mm._k(month, "calls", "deep"), 5)
    fresh.add(mm._k(month, "calls", "standard"), 40)  # 105 calls, light 19%
    # (standard is over too, so either rule brings a scout judgement down)
    assert mm.choose("scout.judge", user("x"), background=True).tier == "light"


def test_budget_modes(fresh, monkeypatch):
    budget = mm.budget_usd()
    assert round(budget, 2) == round(50000 / 88, 2)
    month = mm.month_key()
    now = dt.datetime.now(dt.timezone.utc)
    if mm.month_summary(now)["elapsed_days"] < 1:
        pytest.skip("projection needs a day of the month")
    fresh.add(mm._k(month, "usd", "standard"), budget * 0.9)  # projected far over: economy
    s = mm.month_summary()
    assert s["mode"] in ("economy", "over budget")
    c = mm.choose("orchestrator.nodes.agent", user("think deeply"))
    assert c.tier in ("complex", "standard") and ("economy" in c.reason or "budget" in c.reason)
    fresh.add(mm._k(month, "usd", "standard"), budget * 0.2)  # over budget
    assert mm.month_summary()["mode"] == "over budget"
    assert mm.choose("orchestrator.specialists.writer", user("x")).tier == "standard"
    assert mm.choose("scout.judge", user("x"), background=True).tier == "light"
    monkeypatch.setattr(mm.get_settings(), "llm_budget_hard_stop", True)
    with pytest.raises(mm.BudgetExceeded):
        mm.choose("orchestrator.nodes.agent", user("x"))


def test_cost_uses_cache_prices():
    usage = SimpleNamespace(input_tokens=1000, cache_read_input_tokens=9000, cache_creation_input_tokens=0,
                            output_tokens=500)
    assert round(mm.cost_usd("claude-sonnet-5-5", usage), 6) == round((1000 * 2 + 9000 * 0.10 + 500 * 10) / 1e6, 6)
    assert round(mm.cost_usd("claude-fable-5-2", usage), 6) == round((1000 * 8 + 9000 * 0.2 + 500 * 40) / 1e6, 6)
    big = SimpleNamespace(input_tokens=150_000, cache_read_input_tokens=0, cache_creation_input_tokens=0,
                          output_tokens=0)
    assert round(mm.cost_usd("claude-haiku-5-5", big), 4) == round(150_000 * 0.50 / 1e6, 4)


def test_alerts_go_once_per_level(fresh, monkeypatch):
    sent = []
    from app.reminders import delivery, service

    monkeypatch.setattr(service, "notify_admins", lambda session, kind, title, body: sent.append(title) or 1)
    monkeypatch.setattr(delivery, "email_admins", lambda session, subject, body: 1)
    month = mm.month_key()
    fresh.add(mm._k(month, "usd", "complex"), mm.budget_usd() * 0.85)
    first = mm.check_budget(None)
    assert "Model budget 80% used" in sent and first >= 1
    assert mm.check_budget(None) == 0  # not again this month
    fresh.add(mm._k(month, "usd", "complex"), mm.budget_usd() * 0.2)
    assert mm.check_budget(None) == 1 and sent[-1] == "Model budget used up"
    text = mm.due_alerts(mm.month_summary())[0][2]
    assert "INR" in text and "mix light" in text


def test_targets_parse_percentages(fresh, monkeypatch):
    monkeypatch.setattr(mm.get_settings(), "llm_tier_targets", "light=60,standard=30,complex=5,deep=5")
    assert mm.targets() == pytest.approx({"light": .6, "standard": .3, "complex": .05, "deep": .05})
