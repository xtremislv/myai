"""Prepaid Anthropic API credit: what is left, and alerts before it runs out (owner, 2026-11-17).

The API has no endpoint for the prepaid balance (the Console shows it), so this keeps its own count: the
balance entered after a top-up (`python -m scripts.llm_credit set <USD>`, from the Console's Billing page),
minus the cost of every call since, priced by model_manager.cost_usd (Anthropic list prices, cache reads and
writes separately). It is an estimate: compare it with the Console now and then and set it again.

Alerts (`check`, run by the scheduler every 30 s): when the estimate falls to each level of LLM_CREDIT_ALERTS
(default $10 and $5), once per level per top-up, the admins get a bell notice and an email from mail/notify,
and so do the addresses in LLM_CREDIT_ALERT_EMAILS. When the API itself answers "credit balance is too low",
a "credit used up" alert goes out at once, whatever the estimate says.

State lives in Redis (shared by the gateway and the scheduler, no restart needed to set the balance).
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

K_BALANCE = "llm:credit:balance"   # USD at the time it was set
K_SPENT = "llm:credit:spent"       # USD spent since then
K_SET_AT = "llm:credit:set_at"     # ISO time it was set; also names this top-up's alert flags
K_EMPTY = "llm:credit:empty"       # set when the API refused a call for lack of credit


def _redis():
    from app.llm.model_manager import ledger

    return ledger._r()


def levels() -> list[float]:
    out = []
    for part in (get_settings().llm_credit_alerts or "").split(","):
        try:
            out.append(float(part.strip().lstrip("$")))
        except ValueError:
            continue
    return sorted({v for v in out if v >= 0}, reverse=True)


def set_balance(usd: float, now: dt.datetime | None = None) -> dict[str, Any]:
    """After a top-up (or to correct the estimate): the Console's balance now. Re-arms the alerts."""
    r = _redis()
    if r is None:
        raise RuntimeError("Redis is not reachable: the credit balance lives there")
    stamp = (now or dt.datetime.now(dt.timezone.utc)).isoformat()  # names the alert flags: must be unique
    pipe = r.pipeline()
    pipe.set(K_BALANCE, repr(float(usd)))
    pipe.set(K_SPENT, "0")
    pipe.set(K_SET_AT, stamp)
    pipe.delete(K_EMPTY)
    pipe.execute()
    return status()


def spend(usd: float) -> None:
    """Called for every model call (model_manager.record)."""
    if not usd:
        return
    r = _redis()
    if r is None:
        return
    try:
        if r.exists(K_BALANCE):
            r.incrbyfloat(K_SPENT, usd)
    except Exception:  # noqa: BLE001 - never fail a model call over the count
        logger.debug("credit count not updated", exc_info=True)


def is_out_of_credit(exc: BaseException) -> bool:
    return "credit balance" in str(exc).lower()


def mark_empty() -> None:
    r = _redis()
    if r is not None:
        try:
            # nx: the first refusal names the alert; later ones (every call fails) don't send it again.
            r.set(K_EMPTY, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), nx=True)
        except Exception:  # noqa: BLE001
            pass


def status() -> dict[str, Any]:
    r = _redis()
    if r is None:
        return {"tracked": False, "reason": "Redis not reachable"}
    try:
        raw = r.mget(K_BALANCE, K_SPENT, K_SET_AT, K_EMPTY)
    except Exception:  # noqa: BLE001
        return {"tracked": False, "reason": "Redis not reachable"}
    balance, spent, set_at, empty = (v.decode() if isinstance(v, bytes) else v for v in raw)
    if balance is None:
        return {"tracked": False, "reason": "no balance set (python -m scripts.llm_credit set <USD>)",
                "alert_levels_usd": levels(), "api_refused_for_credit_at": empty}
    balance_f, spent_f = float(balance), float(spent or 0)
    return {"tracked": True, "balance_set_usd": round(balance_f, 4), "set_at": set_at,
            "spent_since_usd": round(spent_f, 4), "remaining_usd": round(balance_f - spent_f, 4),
            "alert_levels_usd": levels(), "api_refused_for_credit_at": empty}


def due_alerts(state: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(flag key, title, body) for every alert the current state calls for (sent or not)."""
    out = []
    note = ("\n\nTop up at console.anthropic.com > Billing, then tell the server the new balance:\n"
            "  cd /root/myai && .venv/bin/python -m scripts.llm_credit set <balance in USD>")
    if state.get("api_refused_for_credit_at"):
        out.append((f"llm:credit:alerted:empty:{state['api_refused_for_credit_at']}",
                    "Anthropic API credit used up: the assistant cannot answer",
                    "The Anthropic API refused a call because the credit balance is too low "
                    f"(at {state['api_refused_for_credit_at']} UTC). Chats, scouts and summaries fail until "
                    "credit is added." + note))
    if not state.get("tracked"):
        return out
    left = state["remaining_usd"]
    for level in levels():
        if left <= level:
            out.append((f"llm:credit:alerted:{level:g}:{state['set_at']}",
                        f"Anthropic API credit low: about ${max(left, 0):.2f} left",
                        f"Estimated credit left: ${max(left, 0):.2f} (alert level ${level:g}).\n"
                        f"Balance set to ${state['balance_set_usd']:.2f} at {state['set_at']}; "
                        f"spent since: ${state['spent_since_usd']:.2f} (estimated from list prices)." + note))
    return out


def check(session) -> int:
    """Send the alerts that are due and not sent yet. Returns how many went out."""
    from app.llm.model_manager import ledger
    from app.reminders import delivery, service

    sent = 0
    for flag, title, body in due_alerts(status()):
        if not ledger.once(flag):
            continue
        service.notify_admins(session, "alert", title, body)
        try:
            delivery.email_admins(session, f"AI Platform: {title}", body)
            extra = [a.strip() for a in (get_settings().llm_credit_alert_emails or "").split(",") if "@" in a]
            if extra:
                delivery.email_addresses(session, extra, f"AI Platform: {title}", body)
        except Exception:  # noqa: BLE001 - the bell notice is saved; mail is best effort
            logger.warning("could not email the credit alert", exc_info=True)
        logger.warning("credit alert sent: %s", title)
        sent += 1
    return sent
