"""What the platform's model calls would cost on Anthropic's API, by task, from the usage log.

    .venv/bin/python -m scripts.llm_cost                    # everything logged
    .venv/bin/python -m scripts.llm_cost --hours 24         # the last day
    .venv/bin/python -m scripts.llm_cost --by task          # each calling function instead of areas

With LLM_PROVIDER=anthropic the log holds Claude's own counts (model "m", cache reads "cached"); the last
table then shows what was actually spent per model (cache writes are priced as plain input: a slight
underestimate).

Reads LLM_USAGE_LOG (JSON lines written by app/llm/client.py: task, prompt and completion tokens, never
content). Prices are USD per million tokens, Anthropic first-party API, as of 2026-11-16 (check
https://platform.claude.com/docs/en/about-claude/pricing before relying on them). Estimates, because:
  - token counts are Gemma's: Claude's tokenizers count the same text differently (TOKENIZER_FACTOR);
  - Claude Opus 5.5 always thinks: its output gets THINKING_FACTOR extra (Sonnet 5.5 can run without
    thinking via thinking={"type": "between_tools"}, Haiku 5.5 with thinking disabled at effort <= high);
  - prompt caching: CACHED[area] of the input is assumed read from cache (5-minute cache, writes at 1.25x
    for the first use of a prefix); the stable part is the tool definitions and system prompt.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

from app.config import get_settings

PRICES = {  # input, output, cache read, cache write (5 min)
    "Claude Opus 5.5": (4.00, 20.00, 0.20, 5.00),
    "Claude Sonnet 5.5": (2.00, 10.00, 0.10, 2.50),
    "Claude Haiku 5.5": (0.10, 0.50, 0.01, 0.125),  # cache read assumed 0.1x: not in the docs checked
}
TOKENIZER_FACTOR = 1.15
THINKING_FACTOR = {"Claude Opus 5.5": 1.0, "Claude Sonnet 5.5": 0.0, "Claude Haiku 5.5": 0.0}  # extra output share

# Calling function -> area of the platform. Unknown tasks are shown under their own name.
AREAS = [
    ("request_manager.guards.triage", "chat: sorting each message (triage)"),
    ("request_manager.guards", "chat: safety and order checks"),
    ("orchestrator.toolsel", "chat: picking tool groups"),
    ("orchestrator.nodes", "chat: assistant (single agent)"),
    ("orchestrator.specialists", "chat: multi-step (supervisor + specialists)"),
    ("orchestrator.agents", "chat: team mode"),
    ("orchestrator.tools", "chat: documents from templates"),
    ("sharing", "chat: sharing checks"),
    ("scout.fresh_ideas", "scouts: search ideas"),
    ("scout.candidates", "scouts: picking products from results"),
    ("scout.shortlist", "scouts: shortlist"),
    ("scout.judge", "scouts: judging a product"),
    ("scout.learned_rules", "scouts: rules from reviews"),
    ("scout.already_covered", "scouts: already covered by us?"),
    ("scout.from_hit", "scouts: competitor hit ideas"),
    ("scout", "scouts: other"),
    ("research", "research jobs (web_research)"),
    ("bigsummary", "long-file summaries"),
    ("background", "background tasks"),
    ("mail", "mail summaries and digests"),
]
# Share of input that a well-built Claude integration would read from the prompt cache, by area.
CACHED = {"chat: assistant (single agent)": 0.75, "chat: multi-step (supervisor + specialists)": 0.6,
          "chat: team mode": 0.6, "chat: sorting each message (triage)": 0.5, "chat: picking tool groups": 0.4}
CACHED_DEFAULT = 0.2
MODEL_NAMES = {"claude-opus-5-5": "Claude Opus 5.5", "claude-sonnet-5-5": "Claude Sonnet 5.5",
               "claude-haiku-5-5": "Claude Haiku 5.5"}


def area(task: str) -> str:
    return next((name for prefix, name in AREAS if task.startswith(prefix)), task)


def cost(model: str, tokens_in: float, tokens_out: float, cached_share: float) -> float:
    price_in, price_out, price_read, price_write = PRICES[model]
    tin = tokens_in * TOKENIZER_FACTOR
    tout = tokens_out * TOKENIZER_FACTOR * (1 + THINKING_FACTOR[model])
    read = tin * cached_share
    write = tin * cached_share * 0.05  # the first use of a prefix (or after a 5-minute gap) writes it
    fresh = tin - read - write
    return (fresh * price_in + read * price_read + write * price_write + tout * price_out) / 1e6


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=0, help="only the last N hours (0 = everything)")
    ap.add_argument("--by", choices=("area", "task"), default="area")
    ap.add_argument("--log", default=get_settings().llm_usage_log)
    args = ap.parse_args()
    path = Path(args.log or "")
    if not args.log or not path.exists():
        raise SystemExit("no usage log: set LLM_USAGE_LOG in .env and restart the gateway and scheduler")
    since = time.time() - args.hours * 3600 if args.hours else 0
    rows: dict[str, list[float]] = defaultdict(lambda: [0, 0, 0])
    spent: dict[str, list[float]] = defaultdict(lambda: [0, 0, 0, 0])  # Claude calls: calls, in, cached, out
    first = last = None
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r["t"] < since:
            continue
        first, last = first or r["t"], r["t"]
        key = area(r["task"]) if args.by == "area" else r["task"]
        rows[key][0] += 1
        rows[key][1] += r["in"]
        rows[key][2] += r["out"]
        if str(r.get("m", "")).startswith("claude"):
            row = spent[r["m"]]
            row[0] += 1
            row[1] += r["in"]
            row[2] += r.get("cached") or 0
            row[3] += r["out"]
    if not rows:
        raise SystemExit("nothing logged in that window")
    span_days = max((last - first) / 86400, 1 / 24)
    models = list(PRICES)
    print(f"{len(rows)} groups, {span_days * 24:.1f} h of usage; costs scaled to 30 days of the same use\n")
    head = f"{'':48} {'calls':>6} {'input':>9} {'output':>8} " + " ".join(f"{m.replace('Claude ', ''):>12}" for m in models)
    print(head)
    totals = [0.0] * len(models)
    for key, (calls, tin, tout) in sorted(rows.items(), key=lambda kv: -kv[1][1]):
        share = CACHED.get(key if args.by == "area" else area(key), CACHED_DEFAULT)
        monthly = [cost(m, tin, tout, share) * 30 / span_days for m in models]
        totals = [a + b for a, b in zip(totals, monthly)]
        print(f"{key[:48]:48} {calls:6.0f} {tin / 1e6:8.2f}M {tout / 1e3:7.0f}K "
              + " ".join(f"${c:11,.2f}" for c in monthly))
    print(f"{'TOTAL per 30 days':48} {'':6} {'':9} {'':8} " + " ".join(f"${c:11,.2f}" for c in totals))
    if spent:
        print(f"\nActually spent on Claude in this window ({span_days * 24:.1f} h):")
        total = 0.0
        for m, (calls, tin, cached, tout) in sorted(spent.items()):
            name = next((v for k, v in MODEL_NAMES.items() if m.startswith(k)), None)
            if name is None:
                print(f"  {m}: {calls:.0f} calls, no price known")
                continue
            p_in, p_out, p_read, _ = PRICES[name]
            usd = ((tin - cached) * p_in + cached * p_read + tout * p_out) / 1e6
            total += usd
            print(f"  {m:24} {calls:6.0f} calls {tin / 1e6:8.2f}M in ({cached / max(tin, 1):4.0%} cached) "
                  f"{tout / 1e3:7.0f}K out  ${usd:,.2f}")
        print(f"  {'total':24} ${total:,.2f}  (about ${total * 30 / span_days:,.2f} per 30 days at this rate)")


if __name__ == "__main__":
    main()
