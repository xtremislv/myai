"""Scouts in the chat (owner, 2026-11-16): look up a department's scouts and their findings, and change a
scout's settings, which pauses for an approver's go-ahead first (`update_scout` is a critical tool in
packs.py; `update_scout_precheck` refuses a bad change before anyone is asked).

Access is the Scouts page's (`scout.allowed`: the department's `Scout/` files, members only). At work only the
scouts of the department doing the work are reachable, so one department's scouts never reach another's run.
Findings are model summaries of web pages: they go to the model as untrusted content."""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from app import scout as scouts
from app.acl.models import Action, Subject
from app.db.session import Scout, ScoutFinding, ScoutRun, SessionLocal

STATES = ("found", "new", "kept", "rejected", "below", "all")
SORTS = ("score", "newest", "price_low", "price_high")


def _subject(state: dict[str, Any]) -> Subject:
    return Subject(id=state.get("subject_id", "anonymous"), roles=tuple(state.get("subject_roles", [])))


def _reach(session, state: dict[str, Any], action: Action = Action.READ) -> list[Scout] | str:
    dept = (state.get("scratchpad") or {}).get("department")
    if not dept:
        return "ERROR: scouts are company data: ask about them in a work request for your department."
    subject = _subject(state)
    if not scouts.allowed(session, subject, dept, Action.READ):
        return f"ACCESS DENIED: {dept} has no scouts you can see (scouts are for the department's members only)."
    if action != Action.READ and not scouts.allowed(session, subject, dept, action):
        return "ACCESS DENIED: only the department's members can change its scouts."
    return session.query(Scout).filter(Scout.department == dept).order_by(Scout.created_at).all()


def _pick(rows: list[Scout], name: str) -> Scout | str:
    want = " ".join(str(name or "").split()).lower()
    if not rows:
        return "No scouts here yet. They are made on the Scouts page."
    if not want:
        if len(rows) == 1:
            return rows[0]
        return "ERROR: name the scout: " + ", ".join(r.name for r in rows)
    exact = [r for r in rows if want in (r.id, r.name.lower())]
    near = exact or [r for r in rows if want in r.name.lower() or r.name.lower() in want]
    if len(near) == 1:
        return near[0]
    if len(near) > 1:
        return "ERROR: which one? " + ", ".join(r.name for r in near)
    return f"ERROR: no scout called {name!r}. Scouts: " + ", ".join(r.name for r in rows)


def _when(t: dt.datetime | None) -> str:
    return t.strftime("%Y-%m-%d %H:%M UTC") if t else "never"


def _rule(st: dict) -> str:
    counted = len(scouts._counted(st["criteria"]))
    rule = (f"passes when at least {st['min_met']} of its {counted} scored criteria are met"
            if st["pass_rule"] == "count" else f"weighted score at least {st['threshold']} and every minimum met")
    required = [c["name"] for c in st["criteria"] if c["required"]]
    return rule + (f"; required: {', '.join(required)}" if required else "")


def _budget(st: dict) -> str:
    parts = [f"cap {scouts.rupees(st['price_cap'])}" if st["price_cap"] else "no price cap"]
    if st["premium_from"]:
        parts.append(f"over {scouts.rupees(st['premium_from'])} needs score {st['premium_score']}+"
                     + (f" and {', '.join(st['premium_needs'])}" if st["premium_needs"] else ""))
    return "; ".join(parts)


def _counts(session, scout_id: str) -> dict[str, int]:
    out = {s: 0 for s in ("new", "kept", "rejected", "below")}
    for (st,) in session.query(ScoutFinding.status).filter(ScoutFinding.scout_id == scout_id):
        out[st] = out.get(st, 0) + 1
    return out


def scout_info(state: dict[str, Any], scout: str = "") -> str:
    """The department's scouts at a glance, or one scout's full settings and recent rounds."""
    with SessionLocal() as session:
        rows = _reach(session, state)
        if isinstance(rows, str):
            return rows
        if not scout:
            if not rows:
                return "No scouts here yet. They are made on the Scouts page."
            out = []
            for r in rows:
                st, c = scouts.clean_settings(r.settings), _counts(session, r.id)
                status = "searching now" if r.running_since else "active" if r.enabled else "paused"
                out.append(f"- {r.name} ({status}): {_rule(st)}. Budget: {_budget(st)}. Finds: {c['new']} new, "
                           f"{c['kept']} kept, {c['rejected']} rejected, {c['below']} below threshold. Last round "
                           f"{_when(r.last_run_at)}, next {_when(r.next_run_at) if r.enabled else 'paused'}.")
            return "Scouts:\n" + "\n".join(out) + "\nscout_info(scout=<name>) shows one in full."
        row = _pick(rows, scout)
        if isinstance(row, str):
            return row
        st, c = scouts.clean_settings(row.settings), _counts(session, row.id)
        crit = "\n".join(f"  - {x['name']} (weight {x['weight']}, minimum score {x['min']}"
                         f"{', required' if x['required'] else ''}): {x['description']}" for x in st["criteria"])
        runs = (session.query(ScoutRun).filter(ScoutRun.scout_id == row.id)
                .order_by(ScoutRun.started_at.desc()).limit(5).all())
        rules = scouts._rules_cache.get(row.id, ("", ""))[1]
        video = ("required" if st["require_video"]
                 else f"not required (without one a find needs a score of {st['no_video_score']})")
        return "\n".join([
            f"Scout: {row.name} ({'searching now' if row.running_since else 'active' if row.enabled else 'paused'})",
            f"Brief: {st['brief'] or '(none)'}",
            f"Market: {st['market'] or 'anywhere'}; import sites: {', '.join(st['import_sites']) or 'none'}",
            f"Pass rule: {_rule(st)}",
            f"Budget: {_budget(st)}",
            f"Reference video: {video}",
            "Criteria (each scored 0-10):\n" + crit,
            f"Pace: every {st['interval_minutes']} min, {st['per_round']} products judged per round, "
            f"{st['daily_lookups']} video lookups a day, {st['ideas_per_round']} search ideas + "
            f"{st['fresh_ideas']} new ones per round; sources: {', '.join(st['sources'])}",
            f"Leave out: {st['avoid']}",
            f"Search ideas: {'; '.join(st['ideas'])}",
            f"Finds: {c['new']} new, {c['kept']} kept, {c['rejected']} rejected, {c['below']} below threshold",
            "Rules learned from the team's reviews:\n" + rules if rules else "Rules learned from reviews: none yet.",
            "Recent rounds:\n" + ("\n".join(
                f"  - {_when(x.started_at)}: {x.status}, {x.candidates} candidates, {x.judged} judged, {x.added} added"
                + (f" ({x.notes[:200]})" if x.notes else "") for x in runs) or "  none"),
            f"Report file: {scouts.report_path(row)} in the department's files.",
        ])


def scout_findings(state: dict[str, Any], scout: str = "", status: str = "found", query: str = "",
                   sort: str = "score", limit: int = 15, max_price: int = 0) -> str:
    """A scout's findings with scores, price, links and review reasons."""
    status = status if status in STATES else "found"
    sort = sort if sort in SORTS else "score"
    with SessionLocal() as session:
        rows = _reach(session, state)
        if isinstance(rows, str):
            return rows
        row = _pick(rows, scout)
        if isinstance(row, str):
            return row
        st = scouts.clean_settings(row.settings)
        q = session.query(ScoutFinding).filter(ScoutFinding.scout_id == row.id)
        if status == "found":
            q = q.filter(ScoutFinding.status.in_(("new", "kept")))
        elif status != "all":
            q = q.filter(ScoutFinding.status == status)
        found = q.all()
    words = [w for w in str(query or "").lower().split() if w]
    if words:
        found = [f for f in found if all(w in f"{f.name} {f.summary}".lower() for w in words)]
    if max_price:
        found = [f for f in found if (p := scouts.price_inr(f.price)) is not None and p <= max_price]
    price = lambda f: scouts.price_inr(f.price)  # noqa: E731
    order = {"score": lambda f: (-f.total, -f.found_at.timestamp() if f.found_at else 0),
             "newest": lambda f: -(f.found_at.timestamp() if f.found_at else 0),
             "price_low": lambda f: (price(f) is None, price(f) or 0),
             "price_high": lambda f: (price(f) is None, -(price(f) or 0))}[sort]
    found.sort(key=order)
    total = len(found)
    found = found[:max(1, min(int(limit or 15), 50))]
    if not found:
        return f"No {status} findings for {row.name}" + (f" matching '{query}'" if query else "") + "."
    lines = []
    for i, f in enumerate(found, 1):
        hits = scouts.met(f.scores or {}, st)
        lines.append(
            f"{i}. {f.name} [{f.status}] score {f.total:.1f}/10" + (f", {f.price}" if f.price else ", no price")
            + f"\n   Meets: {', '.join(hits) or 'none'}"
            + (f"\n   Video: {f.video_url} ({f.video_views:,} views)" if f.video_url else "\n   No reference video")
            + (f"\n   Product: {f.product_url}" if f.product_url else "")
            + (f"\n   {' '.join((f.summary or '').split())[:400]}" if f.summary else "")
            + (f"\n   Not kept because: {f.why.split('Not kept:')[-1].strip()[:300]}" if "Not kept:" in (f.why or "") else "")
            + (f"\n   Team's reason: {f.review_note}" if f.review_note else ""))
    from app.orchestrator.tools import untrusted

    head = f"{row.name}: {total} {status} finding(s)" + (f", showing {len(found)}" if total > len(found) else "")
    return head + "\n" + untrusted("scout findings (summaries of web pages)", "\n".join(lines))


def _parse(changes: str | dict) -> dict:
    if isinstance(changes, dict):
        return changes
    try:
        data = json.loads(changes or "")
    except json.JSONDecodeError:
        raise ValueError('changes must be a JSON object, e.g. {"price_cap": 80000} or '
                         '{"criteria": {"Camera wow": {"min": 8}}}') from None
    if not isinstance(data, dict):
        raise ValueError("changes must be a JSON object")
    return data


def _planned(session, state: dict[str, Any], scout: str, changes) -> tuple[Scout, dict, bool | None, list[str]] | str:
    rows = _reach(session, state, Action.WRITE)
    if isinstance(rows, str):
        return rows
    row = _pick(rows, scout)
    if isinstance(row, str):
        return row
    try:
        new, enabled, lines = scouts.apply_changes(row.settings or {}, _parse(changes))
    except ValueError as e:
        return f"ERROR: {e}"
    return row, new, enabled, lines


def update_scout_precheck(state: dict[str, Any], scout: str = "", changes: str = "", why: str = "") -> str | None:
    """A refusal before the approver is asked (bad scout, no access, invalid or empty change), or None.
    No side effects: it re-runs on resume."""
    with SessionLocal() as session:
        planned = _planned(session, state, scout, changes)
    return planned if isinstance(planned, str) else None


def update_scout(state: dict[str, Any], scout: str = "", changes: str = "", why: str = "") -> str:
    """Change a scout's settings; runs only after an approver's go-ahead (critical tool)."""
    with SessionLocal() as session:
        planned = _planned(session, state, scout, changes)
        if isinstance(planned, str):
            return planned
        row, new, enabled, lines = planned
        old = scouts.clean_settings(row.settings)
        now = dt.datetime.now(dt.timezone.utc)
        if new["interval_minutes"] != old["interval_minutes"] and row.last_run_at:
            row.next_run_at = row.last_run_at + dt.timedelta(minutes=new["interval_minutes"])
        row.settings = new
        moved = scouts.rescore(session, row)  # judged finds follow the new rules at once
        if enabled is not None:
            row.enabled = enabled
            if enabled and row.next_run_at is None:
                row.next_run_at = now
        row.updated_by = _subject(state).id
        row.updated_at = now
        session.commit()
        try:
            scouts.save_report(session, row)
        except Exception:  # noqa: BLE001 - the next round writes it anyway
            pass
        name = row.name
    return (f"Done: {name} updated.\n- " + "\n- ".join(lines)
            + (f"\n{moved} judged find(s) moved between Found and Below threshold under the new rules." if moved else ""))
