"""Measure toolsel.classify against the live model with a labelled set (tests/eval/toolgroup_cases.jsonl).

    .venv/bin/python -m scripts.eval_toolgroups            # 2 runs
    .venv/bin/python -m scripts.eval_toolgroups --runs 1 --show-all

The error that matters is a MISS: a group the message needs was not picked, so the agent has to
ask for it with more_tools (one extra model call). Extra groups only cost a little speed. Run it
after changing toolsel.PROMPT, its DESCRIPTIONS or GROUPS. Needs vLLM running (LLM_BASE_URL in .env).
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.orchestrator import toolsel

CASES = Path(__file__).resolve().parent.parent / "tests" / "eval" / "toolgroup_cases.jsonl"


def _context(case: dict) -> list[dict]:
    """"Assistant: ..." / "User: ..." lines as messages."""
    out = []
    for part in (case.get("context") or "").split("\n"):
        role, _, text = part.partition(":")
        if text and role.strip().lower() in ("user", "assistant"):
            out.append({"role": role.strip().lower(), "content": text.strip()})
    return out


def _one(case: dict) -> tuple[dict, frozenset | None, float]:
    start = time.perf_counter()
    got = toolsel.classify(case["msg"], _context(case))
    return case, got, time.perf_counter() - start


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--show-all", action="store_true")
    args = parser.parse_args(argv)
    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    worst = 0
    for run in range(1, args.runs + 1):
        with ThreadPoolExecutor(6) as pool:
            results = list(pool.map(_one, cases))
        misses, extra, unparsed = [], 0, 0
        for case, got, _ in results:
            need = set(case["need"])
            if got is None:
                unparsed += 1
                misses.append((case["id"], case["msg"], sorted(need), "unparsed: every tool shown"))
            elif not need <= got:
                misses.append((case["id"], case["msg"], sorted(need), sorted(got)))
            else:
                extra += len(got - need)
            if args.show_all:
                print(f"  {case['id']} {sorted(got) if got is not None else None} <- {case['msg']}")
        avg = sum(t for _, _, t in results) / len(results)
        print(f"run {run}: all needed groups picked {len(cases) - len(misses)}/{len(cases)}, "
              f"unparsed {unparsed}, extra groups {extra}, {avg:.2f}s per message")
        for miss in misses:
            print("   MISS", *miss)
        worst = max(worst, len(misses))
    return 1 if worst > len(cases) // 10 else 0


if __name__ == "__main__":
    raise SystemExit(main())
