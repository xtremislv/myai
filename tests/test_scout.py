"""Scouts (app/scout.py, app/api/routes_scouts.py): rounds, judging, the department file, the API."""
from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import scout as scouts
from app.auth.security import issue_token
from app.db.session import (
    Base,
    Notification,
    Scout,
    ScoutFinding,
    ScoutMute,
    ScoutRun,
    User,
    get_session,
)
from app.departments.registry import seed_departments
from app.main import app
from app.storage import objects


@pytest.fixture
def db():
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, expire_on_commit=False)
    with Session() as s:
        seed_departments(s)
        s.add(User(id="r1", username="r1", password_hash="x", roles=["researcher"], prefs={}))
        s.add(User(id="r2", username="r2", password_hash="x", roles=["researcher"], prefs={}))
        s.add(User(id="h1", username="h1", password_hash="x", roles=["hr"], prefs={}))
        s.commit()
    return Session


@pytest.fixture
def files(monkeypatch):
    blobs: dict[str, bytes] = {}

    class FakeStore:
        def __init__(self, acl):
            self._acl = acl

        def put(self, subject, key, data, content_type="x"):
            from app.acl.models import Action

            self._acl.enforce(subject, Action.WRITE, objects.resource_for(key))
            blobs[key] = data

    monkeypatch.setattr(objects, "ObjectStore", FakeStore)
    monkeypatch.setattr(objects, "system_object_size", lambda k: len(blobs[k]) if k in blobs else None)
    monkeypatch.setattr("app.storage.deptfiles._keep_old", lambda k: True)
    monkeypatch.setattr("app.reminders.service.notes.encrypt", lambda s, text, aad: text)
    return blobs


VIDEO = ("https://www.youtube.com/watch?v=aaaaaaaaaaa", "This lamp turns into a speaker!", 2_500_000, "Top comments: wow")


def fake_tools(scores: dict[str, dict[str, float]], videos: dict[str, tuple | None] | None = None, calls=None):
    calls = calls if calls is not None else []

    def chat(system: str, user: str, max_tokens: int) -> str:
        calls.append(system[:40])
        if system.startswith("You plan searches"):
            return '["fresh idea one"]'
        if system.startswith("From search results"):
            return json.dumps([{"name": n, "what": "a gadget", "link": ""} for n in scores])
        if system.startswith("You pick which products"):
            return json.dumps(list(range(len(scores), 0, -1)))  # best last-found first
        if system.startswith("You judge one product"):
            name = next(n for n in scores if f"Product: {n} " in user)
            return json.dumps({"is_specific_product": True, "scores": scores[name], "summary": f"{name} transforms.",
                               "why": "fits", "product_link": f"https://shop.in/products/{name.lower().replace(' ', '-')}",
                               "price": "₹999"})
        raise AssertionError(system[:60])

    vids = videos or {}
    return scouts.Tools(youtube_page=lambda q: f"videos for {q}", reddit=lambda q: f"threads for {q}",
                        stores=lambda q: f"store results for {q}", web=lambda q: f"web results for {q}",
                        video=lambda name: vids.get(name, VIDEO), chat=chat)


HIGH = {c["name"]: 9 for c in scouts.DEFAULTS["criteria"]}
LOW = {**{c["name"]: 3 for c in scouts.DEFAULTS["criteria"]}, "Daily use": 9}  # meets only one criterion


def _scout(db, **settings) -> Scout:
    with db() as s:
        return scouts.create(s, "researcher", "Rare gadget finds", "r1", {"sources": ["youtube", "web"], **settings})


def test_a_round_judges_new_products_and_keeps_only_those_that_pass(db, files):
    sc = _scout(db)
    tools = fake_tools({"Lumi Lamp Speaker": HIGH, "Fold Mug": LOW, "Plain Cup": HIGH},
                       videos={"Plain Cup": None})
    run = scouts.run_now(db, sc.id, tools)
    assert run.status == "done" and run.candidates == 3 and run.judged == 3 and run.added == 1
    assert "fresh idea one" in run.queries and run.queries[0] == scouts.DEFAULTS["ideas"][0]
    with db() as s:
        rows = {f.name: f for f in s.query(ScoutFinding)}
        assert rows["Lumi Lamp Speaker"].status == "new" and rows["Lumi Lamp Speaker"].total == 9.0
        assert rows["Lumi Lamp Speaker"].video_url == VIDEO[0] and rows["Lumi Lamp Speaker"].product_url.startswith("https://shop.in/")
        assert rows["Fold Mug"].status == "below" and "meets 1 of the 2 criteria needed (Daily use)" in rows["Fold Mug"].why
        assert rows["Plain Cup"].status == "below" and "no reference video" in rows["Plain Cup"].why
        after = s.get(Scout, sc.id)
        assert after.running_since is None and after.next_run_at > after.last_run_at
        # every research member hears about it
        assert sorted(n.owner_id for n in s.query(Notification)) == ["r1", "r2"]
    # the department's file has the find, the rest, the rounds and the settings
    [key] = [k for k in files if k.startswith("shared/researcher/Scout/")]
    wb = load_workbook(io.BytesIO(files[key]))
    assert wb.sheetnames == ["Findings", "Below threshold", "Runs", "Settings"]
    assert [r[2] for r in wb["Findings"].iter_rows(min_row=2, values_only=True)] == ["Lumi Lamp Speaker"]
    assert len(list(wb["Below threshold"].iter_rows(min_row=2))) == 2


def test_nothing_is_judged_twice_and_rejections_steer_the_next_round(db, files):
    sc = _scout(db)
    scouts.run_now(db, sc.id, fake_tools({"Lumi Lamp Speaker": HIGH}))
    with db() as s:
        f = s.query(ScoutFinding).one()
        f.status = "rejected"
        s.commit()
    seen: list[str] = []
    tools = fake_tools({"Lumi Lamp Speaker": HIGH, "Cube Charger": HIGH})
    real_chat = tools.chat

    def spy(system, user, n):
        seen.append(system)
        return real_chat(system, user, n)

    tools.chat = spy
    run = scouts.run_now(db, sc.id, tools)
    assert run.judged == 1 and run.added == 1  # only the new one
    assert any("rejected: Lumi Lamp Speaker" in s for s in seen if s.startswith("From search results"))


def test_the_daily_lookup_budget_stops_judging(db, files):
    sc = _scout(db, daily_lookups=1)
    run = scouts.run_now(db, sc.id, fake_tools({"A One": HIGH, "B Two": HIGH, "C Three": HIGH}))
    assert run.judged == 1
    run = scouts.run_now(db, sc.id, fake_tools({"D Four": HIGH}))
    assert run.judged == 0 and "budget" in run.notes


def test_a_running_scout_is_not_started_twice_and_stale_runs_are_freed(db, files):
    import datetime as dt

    sc = _scout(db)
    with db() as s:
        assert scouts.claim(s, sc.id) and not scouts.claim(s, sc.id)
        assert scouts.due(s) == []
        row = s.get(Scout, sc.id)
        row.running_since = dt.datetime.now(dt.timezone.utc) - scouts.STALE_RUN - dt.timedelta(minutes=1)
        s.commit()
        assert scouts.due(s) == [sc.id]


def test_settings_are_checked_and_kept_in_range():
    st = scouts.clean_settings({"threshold": 42, "interval_minutes": 1, "sources": ["youtube", "bogus"],
                                "criteria": [{"name": " Fun ", "weight": 9, "min": -3}], "ideas": "a\n\nb"})
    assert st["threshold"] == 10 and st["interval_minutes"] == 15 and st["sources"] == ["youtube"]
    assert st["criteria"] == [{"name": "Fun", "description": "", "weight": 9, "min": 0.0, "required": False}]
    assert st["ideas"] == ["a", "b"]
    assert scouts.clean_settings({"criteria": [{"name": "x", "weight": 500}]})["criteria"][0]["weight"] == 100
    with pytest.raises(ValueError):
        scouts.clean_settings({"criteria": [{"name": ""}]})


# ---- the API ----
@pytest.fixture
def client(db, files):
    def _get_session():
        with db() as s:
            yield s

    app.dependency_overrides[get_session] = _get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _as(uid, roles):
    return {"authorization": "Bearer " + issue_token(uid, uid, roles)}


def test_members_make_edit_and_review_scouts_others_cannot_see_them(client, db):
    r1, hr = _as("r1", ["researcher"]), _as("h1", ["hr"])
    assert "researcher" in client.get("/v1/scouts/defaults", headers=r1).json()["departments"]
    made = client.post("/v1/scouts", json={"name": "Rare gadget finds", "department": "researcher"}, headers=r1)
    assert made.status_code == 201, made.text
    sid = made.json()["id"]
    assert client.post("/v1/scouts", json={"name": "x", "department": "researcher"}, headers=hr).status_code == 403
    assert client.get("/v1/scouts", headers=hr).json() == []
    assert client.get(f"/v1/scouts/{sid}", headers=hr).status_code == 404

    crit = [{"name": "Tiny", "description": "fits a pocket", "weight": 2, "min": 5}]
    upd = client.put(f"/v1/scouts/{sid}", json={"settings": {"threshold": 8, "criteria": crit}, "enabled": False}, headers=r1)
    assert upd.status_code == 200 and upd.json()["settings"]["threshold"] == 8
    assert upd.json()["settings"]["criteria"][0]["name"] == "Tiny" and upd.json()["next_run_at"] is None
    assert client.put(f"/v1/scouts/{sid}", json={"settings": {"criteria": []}}, headers=r1).status_code == 422

    run = client.post(f"/v1/scouts/{sid}/run", headers=r1).json()
    assert run["enabled"] and run["next_run_at"]  # one round, paused again afterwards
    with db() as s:
        s.add(ScoutFinding(id="f1", scout_id=sid, key="cube", name="Cube", status="new", total=8.5, scores={}))
        s.commit()
    assert [f["name"] for f in client.get(f"/v1/scouts/{sid}/findings", headers=r1).json()] == ["Cube"]
    kept = client.patch(f"/v1/scouts/{sid}/findings/f1", json={"status": "kept"}, headers=r1).json()
    assert kept["status"] == "kept" and kept["reviewed_by"] == "r1"
    assert client.delete(f"/v1/scouts/{sid}", headers=r1).status_code == 204
    with db() as s:
        assert s.query(Scout).count() == 0 and s.query(ScoutFinding).count() == 0 and s.query(ScoutRun).count() == 0


def test_the_shortlist_spends_lookups_on_the_most_promising(db, files):
    sc = _scout(db, per_round=1)
    run = scouts.run_now(db, sc.id, fake_tools({"A One": LOW, "B Two": HIGH}))
    assert run.candidates == 2 and run.judged == 1 and run.added == 1  # B Two, ranked first, was judged


def test_an_unreadable_verdict_leaves_the_product_unjudged(db, files):
    sc = _scout(db)
    tools = fake_tools({"A One": HIGH})
    real = tools.chat
    tools.chat = lambda system, user, n: "sorry" if system.startswith("You judge one product") else real(system, user, n)
    run = scouts.run_now(db, sc.id, tools)
    assert run.judged == 0 and "couldn't judge A One" in run.notes
    with db() as s:
        assert s.query(ScoutFinding).count() == 0


def test_two_criteria_are_enough_and_the_rule_can_change_later(db, files):
    pair = {**{c["name"]: 2 for c in scouts.DEFAULTS["criteria"]}, "Rare find": 8, "Unexpected function": 9}
    sc = _scout(db)
    run = scouts.run_now(db, sc.id, fake_tools({"Odd Gizmo": pair}))
    assert run.added == 1  # never seen before + unexpected: two of them, enough
    with db() as s:
        row = s.get(Scout, sc.id)
        row.settings = scouts.clean_settings({**row.settings, "min_met": 3})
        assert scouts.rescore(s, row) == 1
        f = s.query(ScoutFinding).one()
        assert f.status == "below" and "meets 2 of the 3" in f.why
        row.settings = scouts.clean_settings({**row.settings, "pass_rule": "score", "threshold": 2})
        scouts.rescore(s, row)
        assert f.status == "below" and "below minimum" in f.why  # score rule: every criterion must be met
        row.settings = scouts.clean_settings({**row.settings, "pass_rule": "count", "min_met": 2})
        scouts.rescore(s, row)
        assert f.status == "new" and "Not kept" not in f.why


def test_a_video_that_does_not_show_the_product_does_not_count(db, files):
    sc = _scout(db)
    tools = fake_tools({"Prism Cube": HIGH})
    real = tools.chat

    def chat(system, user, n):
        reply = real(system, user, n)
        if system.startswith("You judge one product"):
            data = json.loads(reply)
            data["video_shows_product"] = False
            return json.dumps(data)
        return reply

    tools.chat = chat
    run = scouts.run_now(db, sc.id, tools)
    assert run.added == 0
    with db() as s:
        f = s.query(ScoutFinding).one()
        assert f.video_url == "" and "no reference video" in f.why


# ---- two scouts, own criteria, never mixed (owner, 2026-11-14) ----
GW = {"Extremely affordable": 8, "Solves an everyday problem": 8, "Highly aesthetic": 7,
      "Looks one way, acts another way": 5, "Fresh or little-known": 6, "Available in India": 9}


def test_gadgetwiser_is_weighted_and_needs_india():
    st = scouts.clean_settings(scouts.GADGETWISER)
    assert [c["weight"] for c in st["criteria"]] == [30, 25, 22, 18, 15, 0] and st["market"] == "India"
    total, reasons = scouts.verdict(GW, st, True)
    assert total == 7.04 and reasons == []  # (8*30 + 8*25 + 7*22 + 5*18 + 6*15) / 110
    total, reasons = scouts.verdict({**GW, "Available in India": 2}, st, True)
    assert reasons == ["required but not met: Available in India"]  # a great score can't make up for it
    assert scouts.verdict({**GW, "Extremely affordable": 2, "Solves an everyday problem": 3}, st, True)[1]
    # required works under the count rule too
    tw = scouts.clean_settings({**scouts.TECHWISER_INSTAGRAM, "criteria": [
        *scouts.DEFAULTS["criteria"], {"name": "Safe", "description": "", "weight": 1, "min": 5, "required": True}]})
    assert "required but not met: Safe" in scouts.verdict(HIGH, tw, True)[1]


def test_review_reasons_teach_only_their_own_scout(db, files):
    with db() as s:
        tw = scouts.create(s, "researcher", "TechWiser Instagram", "r1", {**scouts.TECHWISER_INSTAGRAM, "sources": ["web"]})
        gw = scouts.create(s, "researcher", "GadgetWiser", "r1", {**scouts.GADGETWISER, "sources": ["web"]})
        s.add(ScoutFinding(id="t1", scout_id=tw.id, key="spin top", name="Spin Top", status="rejected",
                           review_note="too much like a toy"))
        s.add(ScoutFinding(id="g1", scout_id=gw.id, key="cable clip", name="Cable Clip", status="kept",
                           review_note="cheap and fixes messy desks"))
        s.commit()
        assert "too much like a toy" in scouts.lessons(s, tw.id) and "Cable Clip" not in scouts.lessons(s, tw.id)
        assert "KEPT: Cable Clip (reason: cheap and fixes messy desks)" in scouts.lessons(s, gw.id)
    prompts: list[tuple[str, str]] = []
    web_queries: list[str] = []
    tools = fake_tools({"Desk Cube": GW})
    real = tools.chat

    def chat(system, user, n):
        prompts.append((system, user))
        return real(system, user, n)

    tools.chat = chat
    tools.web = lambda q: web_queries.append(q) or f"web results for {q}"
    run = scouts.run_now(db, gw.id, tools)
    assert run.added == 1
    judged = [p for p in prompts if p[0].startswith("You judge one product")]
    assert judged and "cheap and fixes messy desks" in judged[0][0] and "Market: India" in judged[0][0]
    everything = " ".join(a + b for a, b in prompts)
    assert "too much like a toy" not in everything and "Spin Top" not in everything  # the other scout's
    assert "GadgetWiser" in everything and "Reels" not in everything
    assert "Desk Cube price India" in web_queries  # availability and price where it must be sold
    with db() as s:
        assert {f.scout_id for f in s.query(ScoutFinding).filter(ScoutFinding.name == "Desk Cube")} == {gw.id}


def test_each_person_mutes_each_scout_separately(client, db, files):
    r1, r2 = _as("r1", ["researcher"]), _as("r2", ["researcher"])
    a = client.post("/v1/scouts", json={"name": "TechWiser Instagram", "department": "researcher"}, headers=r1).json()
    b = client.post("/v1/scouts", json={"name": "GadgetWiser", "department": "researcher",
                                        "settings": scouts.GADGETWISER}, headers=r1).json()
    assert client.put(f"/v1/scouts/{a['id']}/mute", json={"muted": True}, headers=r1).json()["muted"] is True
    assert client.get(f"/v1/scouts/{a['id']}", headers=r2).json()["muted"] is False
    assert client.get(f"/v1/scouts/{b['id']}", headers=r1).json()["muted"] is False
    assert client.get(f"/v1/scouts/{b['id']}", headers=r1).json()["settings"]["market"] == "India"
    scouts.run_now(db, a["id"], fake_tools({"Lumi Lamp Speaker": HIGH}))
    with db() as s:
        assert sorted(n.owner_id for n in s.query(Notification)) == ["r2"]  # r1 muted this one
        s.query(Notification).delete()
        s.commit()
    with db() as s:
        row = s.get(Scout, b["id"])
        row.settings = scouts.clean_settings({**row.settings, "sources": ["web"]})
        s.commit()
    scouts.run_now(db, b["id"], fake_tools({"Desk Cube": GW}))
    with db() as s:
        notes = list(s.query(Notification))
        assert sorted(n.owner_id for n in notes) == ["r1", "r2"]  # the other scout still reaches r1
    assert client.put(f"/v1/scouts/{a['id']}/mute", json={"muted": False}, headers=r1).json()["muted"] is False
    with db() as s:
        assert s.query(ScoutMute).count() == 0


def test_a_review_can_say_why_and_lands_in_the_file(client, db, files):
    r1 = _as("r1", ["researcher"])
    sc = _scout(db)
    scouts.run_now(db, sc.id, fake_tools({"Lumi Lamp Speaker": HIGH}))
    with db() as s:
        fid = s.query(ScoutFinding).one().id
    out = client.patch(f"/v1/scouts/{sc.id}/findings/{fid}", json={"status": "rejected", "note": " seen  everywhere "},
                       headers=r1).json()
    assert out["status"] == "rejected" and out["review_note"] == "seen everywhere"
    out = client.patch(f"/v1/scouts/{sc.id}/findings/{fid}", json={"note": "actually fine"}, headers=r1).json()
    assert out["status"] == "rejected" and out["review_note"] == "actually fine"
    assert client.patch(f"/v1/scouts/{sc.id}/findings/{fid}", json={}, headers=r1).status_code == 422
    with db() as s:
        scouts.save_report(s, s.get(Scout, sc.id))
    [key] = [k for k in files if k.startswith("shared/researcher/Scout/")]
    ws = load_workbook(io.BytesIO(files[key]))["Below threshold"]
    header = [c.value for c in ws[1]]
    assert header[-1] == "Review note" and list(ws.iter_rows(min_row=2, values_only=True))[0][-1] == "actually fine"


def test_a_score_labelled_with_extra_words_still_counts():
    st = scouts.clean_settings(scouts.GADGETWISER)

    def chat(system, user, n):
        return json.dumps({"scores": {**{k: 8 for k in GW if k != "Available in India"},
                                      "Available in India (required)": 9}, "summary": "s", "why": "w"})

    out = scouts.judge({"name": "Desk Cube"}, VIDEO, "evidence", st, chat)
    assert out["scores"]["Available in India"] == 9 and out["passed"]
