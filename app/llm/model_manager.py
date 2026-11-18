"""Which Claude model answers each call, and what the month costs (owner's rules, 2026-11-16).

Four tiers:
  light     Claude Haiku 5.5   background and short sorting work: triage, safety checks, tool picking, scout
                               searches, mail and file summaries
  standard  Claude Sonnet 5.5  the assistant's normal answers and tool use, scout judging
  complex   Claude Opus 5.5    multi-step work: supervisor/specialists, team mode, long tool chains, big contexts,
                               analysis/planning/report requests
  deep      Claude Fable 5.2   "extreme" requests: the person asks for deep thinking / an in-depth analysis

Target mix of calls per month: light 50%, standard 35%, complex 10%, deep 5% (LLM_TIER_TARGETS). The targets
for the three paid-up tiers are ceilings: once a month has enough calls (MIN_SAMPLE), a call whose tier is over
its share steps down one tier; and when light is under its share, background work that would use standard goes
light. Nothing is ever stepped *up* just to fill a quota.

Budget: LLM_MONTHLY_BUDGET in LLM_BUDGET_CURRENCY (default 50,000 INR, converted at LLM_USD_RATE). Every call's
cost is computed from its usage (cache reads and writes priced separately) and added to the month's ledger in
Redis, shared by the gateway and the scheduler (in memory if Redis is down). The month's end is projected
from the spend so far; when the projection passes the budget, the manager goes into economy mode (deep and
complex step down one tier), and at 100% spent only light and standard are used (LLM_BUDGET_HARD_STOP=true
refuses calls instead). `check_budget` (run by the scheduler) alerts the admins once per month per level:
projected over budget, 80% spent, 100% spent: on the admin's bell and by email from mail/notify.
"""
from __future__ import annotations

import calendar
import datetime as dt
import logging
import re
import threading
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings

logger = logging.getLogger(__name__)

TIERS = ("light", "standard", "complex", "deep")
# USD per million tokens: input, output, cache read, cache write (5 minutes). Anthropic list prices,
# 2026-11-16; check https://platform.claude.com/docs/en/about-claude/pricing before relying on them.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),    # prompts over 100K tokens: 0.50 / 2.50 (LONG_HAIKU)
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-fable-5-2": (8.00, 40.00, 0.20, 10.00),
}
LONG_HAIKU = (0.50, 2.50, 0.05, 0.625)
MIN_SAMPLE = 100        # calls this month before the mix is steered
TOLERANCE = 0.02        # a tier may run this far over its share before calls step down
ALERT_LEVELS = ("projected", "80", "100")

# Who asks: app.llm.client.caller_task() names, matched by prefix.
LIGHT_TASKS = ("request_manager.guards", "orchestrator.toolsel", "sharing", "scout.fresh_ideas",
               "scout.candidates", "scout.shortlist", "scout.already_covered", "scout.from_hit", "mail",
               "bigsummary", "orchestrator.context")
# Tasks that write what a person reads: the only ones a request's own words may raise to complex or deep.
ANSWER_TASKS = ("orchestrator.nodes", "orchestrator.specialists", "orchestrator.agents", "research", "background")
COMPLEX_TASKS = ("orchestrator.specialists", "orchestrator.agents", "orchestrator.tools")
DEEP_WORDS = re.compile(
    r"\b(think (deeply|hard|carefully)|deep (thinking|analysis|dive|research|reasoning)|in[- ]depth|"
    r"thorough(ly)? (analy|research|review|investigat)|reason (it )?step by step|exhaustive|use fable|"
    r"extreme(ly)? (detailed|thorough))", re.I)
COMPLEX_WORDS = re.compile(
    r"\b(analy[sz]e|analysis|compare|comparison|strategy|plan (for|out)|business plan|forecast|evaluate|"
    r"audit|contract|negotiat|financial|spreadsheet|report on|pros and cons|trade-?offs?|architecture|"
    r"debug|refactor|write (a |the )?code)", re.I)
COMPLEX_TOOL_ROUNDS = 4        # tool results in this conversation before an answer counts as complex
COMPLEX_CONTEXT_CHARS = 120_000  # ~30-40K tokens of prompt


@dataclass
class Choice:
    tier: str
    model: str
    reason: str


def _settings():
    return get_settings()


def models() -> dict[str, str]:
    s = _settings()
    return {"light": s.llm_light_model, "standard": s.llm_standard_model or s.llm_model,
            "complex": s.llm_complex_model, "deep": s.llm_deep_model}


def targets() -> dict[str, float]:
    raw = dict(zip(TIERS, (0.50, 0.35, 0.10, 0.05)))
    for part in (_settings().llm_tier_targets or "").split(","):
        name, _, value = part.partition("=")
        if name.strip() in raw and value.strip():
            try:
                raw[name.strip()] = float(value) / (100 if float(value) > 1 else 1)
            except ValueError:
                pass
    total = sum(raw.values()) or 1
    return {k: v / total for k, v in raw.items()}


def budget_usd() -> float:
    s = _settings()
    amount = float(s.llm_monthly_budget or 0)
    return amount if s.llm_budget_currency.upper() == "USD" else amount / max(float(s.llm_usd_rate), 0.01)


def money(usd: float) -> str:
    s = _settings()
    if s.llm_budget_currency.upper() == "USD":
        return f"${usd:,.2f}"
    return f"{s.llm_budget_currency.upper()} {usd * float(s.llm_usd_rate):,.0f} (${usd:,.2f})"


# ---- the month's ledger: Redis, shared by every process; memory when Redis is unreachable ----
def month_key(now: dt.datetime | None = None) -> str:
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(_settings().timezone))
    return now.strftime("%Y-%m")


class Ledger:
    def __init__(self, redis_client=None) -> None:
        self._redis = redis_client
        self._mem: dict[str, float] = {}
        self._lock = threading.Lock()

    def _r(self):
        if self._redis is False:
            return None
        if self._redis is None:
            try:
                import redis

                self._redis = redis.Redis.from_url(_settings().redis_url, socket_connect_timeout=0.25,
                                                   socket_timeout=0.25)
            except Exception:  # noqa: BLE001
                self._redis = False
                return None
        return self._redis

    def add(self, key: str, amount: float) -> None:
        r = self._r()
        if r is not None:
            try:
                pipe = r.pipeline()
                pipe.incrbyfloat(key, amount)
                pipe.expire(key, 75 * 86400)
                pipe.execute()
                return
            except Exception:  # noqa: BLE001 - fall back to this process's memory
                pass
        with self._lock:
            self._mem[key] = self._mem.get(key, 0.0) + amount

    def get(self, key: str) -> float:
        r = self._r()
        if r is not None:
            try:
                value = r.get(key)
                return float(value or 0) + self._mem.get(key, 0.0)
            except Exception:  # noqa: BLE001
                pass
        return self._mem.get(key, 0.0)

    def once(self, key: str) -> bool:
        """True the first time this key is claimed (alert levels: one email per month each)."""
        r = self._r()
        if r is not None:
            try:
                return bool(r.set(key, "1", nx=True, ex=75 * 86400))
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            if key in self._mem:
                return False
            self._mem[key] = 1.0
            return True


ledger = Ledger()


def _k(month: str, *parts: str) -> str:
    return ":".join(("llm", month, *parts))


def month_summary(now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    month = month_key(now)
    local = now.astimezone(ZoneInfo(_settings().timezone))
    days = calendar.monthrange(local.year, local.month)[1]
    elapsed = (local.day - 1) + (local.hour * 3600 + local.minute * 60 + local.second) / 86400
    calls = {t: int(ledger.get(_k(month, "calls", t))) for t in TIERS}
    cost = {t: ledger.get(_k(month, "usd", t)) for t in TIERS}
    spent = sum(cost.values())
    total_calls = sum(calls.values())
    # Projection: the spend rate so far over the whole month; too early to trust before ~1 day of data.
    projected = spent * days / max(elapsed, 1.0)
    budget = budget_usd()
    return {
        "month": month, "days": days, "elapsed_days": round(elapsed, 2), "budget_usd": round(budget, 2),
        "spent_usd": round(spent, 6), "projected_usd": round(projected, 2),
        "spent_share": round(spent / budget, 4) if budget else 0.0,
        "projected_share": round(projected / budget, 4) if budget else 0.0,
        "calls": calls, "cost_usd": {t: round(c, 6) for t, c in cost.items()}, "total_calls": total_calls,
        "share": {t: round(calls[t] / total_calls, 4) if total_calls else 0.0 for t in TIERS},
        "targets": {t: round(v, 4) for t, v in targets().items()}, "models": models(),
        "mode": mode(spent, projected, budget, elapsed),
    }


def mode(spent: float, projected: float, budget: float, elapsed_days: float = 30.0) -> str:
    if not budget:
        return "normal"
    if spent >= budget:
        return "over budget"
    if projected > budget and elapsed_days >= 1:  # a first-day burst says little about the month
        return "economy"
    return "normal"


# ---- choosing ----
def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return _text_of(m.get("content"))
    return ""


def _text_of(content) -> str:
    """A message's words: the text parts of a list (a picture is not words, and its base64 is not
    "large context")."""
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return str(content or "")


def base_tier(task: str, messages: list[dict], tools: list | None, background: bool = False) -> tuple[str, str]:
    """The tier this call's work needs, before the monthly mix and budget are applied."""
    if task.startswith(LIGHT_TASKS):
        return "light", "short sorting or background step"
    answer = task.startswith(ANSWER_TASKS)
    if answer:
        text = _last_user_text(messages)[:4000]
        if DEEP_WORDS.search(text):
            return "deep", "the request asks for deep thinking"
        if task.startswith(COMPLEX_TASKS):
            return "complex", "multi-step work"
        tool_results = sum(1 for m in messages if m.get("role") == "tool")
        if tool_results >= COMPLEX_TOOL_ROUNDS:
            return "complex", f"long tool chain ({tool_results} results)"
        size = sum(len(_text_of(m.get("content"))) for m in messages)
        if size >= COMPLEX_CONTEXT_CHARS:
            return "complex", "large context"
        if COMPLEX_WORDS.search(text):
            return "complex", "analysis or planning request"
        return "standard", "assistant answer"
    if task.startswith(COMPLEX_TASKS):
        return "complex", "multi-step work"
    if background:
        return ("standard", "background judgement") if task.startswith(("scout.judge", "scout.learned_rules")) \
            else ("light", "background work")
    return "standard", "default"


def _down(tier: str) -> str:
    return TIERS[max(0, TIERS.index(tier) - 1)]


def steer(tier: str, reason: str, task: str, summary: dict[str, Any]) -> tuple[str, str]:
    """Apply the budget, then the monthly mix."""
    state = summary["mode"]
    if state == "over budget":
        if _settings().llm_budget_hard_stop:
            raise BudgetExceeded(f"monthly model budget used up ({money(summary['spent_usd'])})")
        if tier in ("deep", "complex"):
            return "standard", f"{reason}; budget used up"
        if tier == "standard" and not task.startswith(ANSWER_TASKS):
            return "light", f"{reason}; budget used up"
        return tier, reason
    if state == "economy" and tier in ("deep", "complex"):
        tier, reason = _down(tier), f"{reason}; economy (month projected over budget)"
    if summary["total_calls"] >= MIN_SAMPLE:
        share, target = summary["share"], summary["targets"]
        while tier != "light" and share[tier] > target[tier] + TOLERANCE:
            tier, reason = _down(tier), f"{reason}; {tier} over its {target[tier]:.0%} share"
        if (tier == "standard" and not task.startswith(ANSWER_TASKS)
                and share["light"] < target["light"] - TOLERANCE):
            tier, reason = "light", f"{reason}; light under its share"
    return tier, reason


class BudgetExceeded(RuntimeError):
    pass


# A stuck turn (orchestrator.nodes.stuck_signals): its answer calls step up one tier, at least to complex
# (owner, 2026-11-18: "Opus first", Fable when Opus was already working on it). The budget and the monthly mix
# still apply afterwards (steer), so a month over budget stays on the cheaper models.

escalated: ContextVar[bool] = ContextVar("escalated", default=False)
# What this request has spent on the model so far, in USD ([total]): set fresh by orchestrator.execution for each
# request, added to by record(). The step-up stops at RESCUE_CAP_INR and only uses deep under RESCUE_DEEP_UNDER_INR
# (owner, 2026-11-18, after a stuck WhatsApp request spent ~INR 160 on Fable).
request_spend: ContextVar[list | None] = ContextVar("request_spend", default=None)
RESCUE_CAP_INR = 50
RESCUE_DEEP_UNDER_INR = 25


def spent_inr() -> float:
    spend = request_spend.get()
    return (spend[0] if spend else 0.0) * float(_settings().llm_usd_rate)


def step_up(tier: str) -> str:
    return "complex" if TIERS.index(tier) < TIERS.index("complex") else "deep"


def rescue_tier(tier: str, reason: str, summary: dict[str, Any]) -> tuple[str, str]:
    """The tier for a stuck task's call, from the tier it would get anyway (after the budget and the mix): one up,
    so Haiku or Sonnet becomes Opus, and Opus becomes Fable only while the request has spent under
    RESCUE_DEEP_UNDER_INR. Nothing changes while the month is over (or projected over) budget, or once the request
    has spent RESCUE_CAP_INR."""
    if summary["mode"] in ("over budget", "economy"):
        return tier, f"{reason}; stuck, but no step-up ({summary['mode']})"
    spent = spent_inr()
    if spent >= RESCUE_CAP_INR:
        return tier, f"{reason}; stuck, but this request has spent its INR {RESCUE_CAP_INR} for stepping up"
    up = step_up(tier)
    if up == "deep" and spent >= RESCUE_DEEP_UNDER_INR:
        return tier, f"{reason}; stuck, already on {tier} (over INR {RESCUE_DEEP_UNDER_INR} for deep)"
    return up, f"{reason}; stepped up: the task got stuck"


# When the API fails a call after the SDK's own retries (overloaded, 5xx, timeout): the tier tried once instead.
FALLBACK_TIER = {"light": "standard", "standard": "complex", "complex": "standard", "deep": "complex"}


def choose(task: str, messages: list[dict], tools: list | None = None, requested: str | None = None,
           background: bool = False) -> Choice:
    """The model for this call. `requested`: a Claude model id a caller insists on (kept as is)."""
    if requested and requested.startswith("claude"):
        tier = next((t for t, m in models().items() if m == requested), "standard")
        return Choice(tier, requested, "asked for by the caller")
    if not _settings().llm_smart_routing:
        return Choice("standard", _settings().llm_model, "smart routing off")
    tier, reason = base_tier(task, messages, tools, background)
    summary = month_summary()
    tier, reason = steer(tier, reason, task, summary)
    if escalated.get() and task.startswith(ANSWER_TASKS):
        # From the model it would really use (the mix may have stepped it down), not the one it was meant for.
        tier, reason = rescue_tier(tier, reason, summary)
    ledger.add(_k(month_key(), "calls", tier), 1)
    return Choice(tier, models()[tier], reason)


# ---- costs ----
def cost_usd(model: str, usage: Any) -> float:
    fresh = getattr(usage, "input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    price = next((p for m, p in PRICES.items() if model.startswith(m)), PRICES["claude-opus-5-5"])
    if model.startswith("claude-haiku-5-5") and fresh + read + write > 100_000:
        price = LONG_HAIKU
    p_in, p_out, p_read, p_write = price
    return (fresh * p_in + read * p_read + write * p_write + out * p_out) / 1e6


def record(tier: str, model: str, usage: Any) -> float:
    from app.llm import credit

    usd = cost_usd(model, usage)
    spend = request_spend.get()
    if spend is not None:
        spend[0] += usd
    month = month_key()
    ledger.add(_k(month, "usd", tier), usd)
    credit.spend(usd)
    return usd


# ---- alerts (scripts/scheduler.py runs this every few minutes) ----
def due_alerts(summary: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(level, title, body) for each alert level reached this month and not sent yet."""
    budget = summary["budget_usd"]
    if not budget:
        return []
    out = []
    spent, projected = summary["spent_usd"], summary["projected_usd"]
    mix = ", ".join(f"{t} {summary['share'][t]:.0%} (target {summary['targets'][t]:.0%})" for t in TIERS)
    detail = (f"Spent so far in {summary['month']}: {money(spent)} of {money(budget)} after "
              f"{summary['elapsed_days']:.1f} of {summary['days']} days. Projected month end: {money(projected)}.\n"
              f"Calls: {summary['total_calls']}; mix {mix}.\n")
    if spent >= budget:
        out.append(("100", "Model budget used up",
                    detail + "From now on only Claude Haiku and Sonnet answer"
                    + (" (LLM_BUDGET_HARD_STOP: model calls are refused)" if _settings().llm_budget_hard_stop else "")
                    + ". Raise LLM_MONTHLY_BUDGET or wait for next month."))
    if spent >= 0.8 * budget:
        out.append(("80", "Model budget 80% used", detail + "Economy mode starts if the projection passes the budget."))
    if projected > budget and summary["elapsed_days"] >= 1 and spent >= 0.05 * budget:
        out.append(("projected", "Model spend on course to pass the monthly budget",
                    detail + "Economy mode is on: Fable and Opus calls step down one tier until the projection "
                    "is back under the budget."))
    return out


def check_budget(session) -> int:
    """Send the alerts that are due (once per month each). Returns how many were sent."""
    from app.reminders import delivery, service

    summary = month_summary()
    sent = 0
    for level, title, body in due_alerts(summary):
        if not ledger.once(_k(summary["month"], "alerted", level)):
            continue
        service.notify_admins(session, "alert", title, body)
        try:
            delivery.email_admins(session, f"AI Platform: {title}", body)
        except Exception:  # noqa: BLE001 - the bell notice is saved; mail is best effort
            logger.warning("could not email the budget alert", exc_info=True)
        logger.warning("budget alert sent: %s", title)
        sent += 1
    return sent
