"""Prepaid Anthropic API credit: tell the server the balance after a top-up, or see what it estimates.

    .venv/bin/python -m scripts.llm_credit                 # estimate: balance set, spent since, left, alert levels
    .venv/bin/python -m scripts.llm_credit set 50          # the Console's balance now (Billing page), in USD

Setting it re-arms the alerts (LLM_CREDIT_ALERTS, default $10 and $5 left). No restart needed. app/llm/credit.py
explains how the estimate is made.
"""
from __future__ import annotations

import argparse
import json

from app.llm import credit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")
    p_set = sub.add_parser("set", help="the balance now, in USD")
    p_set.add_argument("usd", type=float)
    args = parser.parse_args()
    if args.cmd == "set":
        if args.usd < 0:
            parser.error("the balance can't be negative")
        state = credit.set_balance(args.usd)
    else:
        state = credit.status()
    print(json.dumps(state, indent=2))


if __name__ == "__main__":
    main()
