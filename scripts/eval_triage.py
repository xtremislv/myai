"""Measure guards.triage against the live model with a labelled set (tests/eval/triage_cases.jsonl).

    .venv/bin/python -m scripts.eval_triage                 # both personas, 3 runs each
    .venv/bin/python -m scripts.eval_triage --runs 1 --show-all

The error that matters most is a personal message sorted as work: it is then stored outside
the user's encryption. That rate is reported first. Needs vLLM running (LLM_BASE_URL in .env).
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.departments.packs import DEFAULT_DEPARTMENTS
from app.request_manager import guards

CASES = Path(__file__).resolve().parent.parent / "tests" / "eval" / "triage_cases.jsonl"
PERSONAS = {
    "admin": {"admin"},
    "staff (logistics+research)": {"logistics", "researcher"},
}


def departments_for(roles: set[str]) -> dict[str, str]:
    """Same rule as routes_chat._departments_of."""
    return {d.name: d.description for d in DEFAULT_DEPARTMENTS if "admin" in roles or d.name in roles}


def judge(case: dict, got: guards.Triage, depts: dict[str, str]) -> list[str]:
    """Problems with one verdict, empty if it is right."""
    problems = []
    want_safety = case.get("safety", "none")
    if want_safety != "none":
        return [] if got.nsfw else ["missed unsafe message"]
    if got.nsfw:
        return ["safe message flagged as unsafe"]

    want_route = case.get("route", "private")
    if case.get("action"):
        if got.action != case["action"]:
            problems.append(f"action {got.action} (wanted {case['action']})")
        return problems
    # Work in a department this person isn't in should still name the real owner, so the
    # Request Manager can refuse it clearly ("not a member of ...").
    if got.route != want_route:
        problems.append(f"route {got.route} (wanted {want_route})")
    elif want_route == "work" and case.get("dept") and got.department not in case["dept"]:
        problems.append(f"department {got.department} (wanted one of {case['dept']})")
    if want_route == "private" and got.action:
        problems.append(f"private message given action {got.action}")
    return problems


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", type=int, default=3, help="repeat each case to catch inconsistency")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--show-all", action="store_true")
    args = ap.parse_args(argv)

    cases = [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]
    jobs = [(p, c) for p in PERSONAS for c in cases for _ in range(args.runs)]

    def run(job):
        persona, case = job
        depts = departments_for(PERSONAS[persona])
        others = {d.name: d.description for d in DEFAULT_DEPARTMENTS if d.name not in depts and d.name != "admin"}
        got = guards.triage(case["msg"], depts, case.get("recent", []), others)
        return persona, case, got, judge(case, got, depts)

    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(run, jobs))

    for persona in PERSONAS:
        rows = [r for r in results if r[0] == persona]
        private = [r for r in rows if r[1].get("route") == "private" and r[1].get("safety", "none") == "none"]
        leaked = [r for r in private if r[2].route == "work"]
        risky = [r for r in private if r[1].get("risky")]
        risky_leaked = [r for r in risky if r[2].route == "work"]
        wrong = [r for r in rows if r[3]]
        print(f"\n=== {persona}: {len(rows)} verdicts ({args.runs} runs x {len(cases)} cases)")
        print(f"  personal sorted as WORK (leaves encryption): {len(leaked)}/{len(private)}"
              f"  [work-sounding personal: {len(risky_leaked)}/{len(risky)}]")
        print(f"  overall correct: {len(rows) - len(wrong)}/{len(rows)}"
              f" ({100 * (len(rows) - len(wrong)) / len(rows):.1f}%)")
        by_kind = Counter(r[1]["id"][0] for r in wrong)
        print("  errors by kind: " + (", ".join(f"{k}={v}" for k, v in sorted(by_kind.items())) or "none")
              + "   (p=personal w=work a=action s=safety)")
        seen = set()
        for _, case, got, problems in (rows if args.show_all else wrong):
            key = (case["id"], tuple(problems))
            if key in seen:
                continue
            seen.add(key)
            times = sum(1 for r in rows if r[1]["id"] == case["id"] and tuple(r[3]) == tuple(problems))
            status = "; ".join(problems) or "ok"
            print(f"   {case['id']} x{times}: {status} | {case['msg'][:70]}")


if __name__ == "__main__":
    main()
