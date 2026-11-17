"""Prepaid credit estimate and its alerts (app/llm/credit.py), on a fake Redis: never the live one."""
from __future__ import annotations

import pytest

from app.llm import credit
from app.llm import model_manager as mm


class FakeRedis:
    def __init__(self):
        self.data: dict[str, bytes] = {}

    def _b(self, v):
        return v if isinstance(v, bytes) else str(v).encode()

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key] = self._b(value)
        return True

    def get(self, key):
        return self.data.get(key)

    def mget(self, *keys):
        return [self.data.get(k) for k in keys]

    def exists(self, key):
        return int(key in self.data)

    def incrbyfloat(self, key, amount):
        value = float(self.data.get(key, b"0")) + amount
        self.data[key] = self._b(repr(value))
        return value

    def expire(self, key, seconds):
        return True

    def delete(self, *keys):
        for k in keys:
            self.data.pop(k, None)

    def pipeline(self):
        outer = self

        class Pipe:
            def __init__(self):
                self.calls = []

            def __getattr__(self, name):
                return lambda *a, **k: self.calls.append((name, a, k))

            def execute(self):
                return [getattr(outer, n)(*a, **k) for n, a, k in self.calls]

        return Pipe()


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(mm, "ledger", mm.Ledger(redis_client=fake))
    return fake


@pytest.fixture
def sent(monkeypatch):
    from app.reminders import delivery, service

    out = {"bell": [], "admins": [], "extra": []}
    monkeypatch.setattr(service, "notify_admins", lambda s, kind, title, body="": out["bell"].append(title) or 1)
    monkeypatch.setattr(delivery, "email_admins", lambda s, subject, body: out["admins"].append(subject) or 1)
    monkeypatch.setattr(delivery, "email_addresses",
                        lambda s, to, subject, body: out["extra"].append((tuple(to), subject)) or len(to))
    return out


def test_untracked_until_a_balance_is_set(redis):
    assert credit.status()["tracked"] is False
    credit.spend(1.0)  # nothing to count against yet
    assert "llm:credit:spent" not in redis.data


def test_spend_counts_down_from_the_balance_set(redis):
    credit.set_balance(50)
    mm.record("standard", "claude-sonnet-5-5", type("U", (), {"input_tokens": 1_000_000, "output_tokens": 100_000})())
    state = credit.status()
    assert state["spent_since_usd"] == pytest.approx(3.0)  # $2 in + $1 out at Sonnet 5.5 list prices
    assert state["remaining_usd"] == pytest.approx(47.0)


def test_alerts_at_10_and_5_dollars_once_each_then_rearmed_by_a_top_up(redis, sent, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "llm_credit_alert_emails", "owner@example.com, not-an-address")
    credit.set_balance(12)
    assert credit.check(None) == 0
    credit.spend(2.5)  # 9.50 left
    assert credit.check(None) == 1 and credit.check(None) == 0
    assert "about $9.50 left" in sent["bell"][0] and sent["admins"] and sent["extra"][0][0] == ("owner@example.com",)
    credit.spend(5)  # 4.50 left
    assert credit.check(None) == 1 and len(sent["bell"]) == 2
    credit.set_balance(60)  # top-up: quiet again, and the levels are armed for the next run-down
    assert credit.check(None) == 0
    credit.spend(51)
    assert credit.check(None) == 1


def test_api_refusal_for_credit_alerts_once(redis, sent):
    err = RuntimeError("Error code: 400 - Your credit balance is too low to access the Anthropic API.")
    assert credit.is_out_of_credit(err) and not credit.is_out_of_credit(RuntimeError("overloaded"))
    credit.mark_empty()
    credit.mark_empty()  # every later call fails the same way: still one alert
    assert credit.check(None) == 1 and credit.check(None) == 0
    assert "used up" in sent["bell"][0]
    credit.set_balance(20)
    assert credit.status()["api_refused_for_credit_at"] is None


def test_alert_levels_from_settings(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "llm_credit_alerts", "5, $10, x, 2")
    assert credit.levels() == [10.0, 5.0, 2.0]
