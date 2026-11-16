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


# ---- TechWiser Instagram: orderable in India, budget cap, premium bar (owner, 2026-11-16) ----
IG = {**HIGH, "Multiple talking points": 9, "Orderable in India": 9}


def test_rupee_prices_are_read_and_written_the_indian_way():
    assert scouts.price_inr("₹54,999") == 54999 and scouts.price_inr("Rs. 1.2 lakh") == 120000
    assert scouts.price_inr("₹74 (MRP ₹162, 54% off)") == 74 and scouts.price_inr("8999 INR") == 8999
    assert scouts.price_inr("$39.00") is None and scouts.price_inr("") is None
    assert scouts.rupees(100000) == "₹1,00,000" and scouts.rupees(999) == "₹999" and scouts.rupees(12345678) == "₹1,23,45,678"


def test_instagram_needs_india_and_keeps_to_the_budget():
    st = scouts.clean_settings(scouts.TECHWISER_INSTAGRAM)
    assert st["market"] == "India" and st["price_cap"] == 100000 and st["premium_from"] == 50000
    assert st["premium_needs"] == ["Multiple talking points", "Camera wow"]
    assert st["import_sites"] == ["ubuy.co.in", "desertcart.in"]
    assert scouts.verdict(IG, st, True, price="₹4,999")[1] == []
    assert scouts.verdict({**IG, "Orderable in India": 3}, st, True, price="₹4,999")[1] == [
        "required but not met: Orderable in India"]
    assert scouts.verdict(IG, st, True, price="₹1,20,000")[1] == ["₹1,20,000 is over the ₹1,00,000 budget"]
    assert scouts.verdict(IG, st, True, price="")[1] == ["no price in ₹ found, so the budget can't be checked"]
    # over ₹50,000: only with a high score, many talking points and camera wow
    assert scouts.verdict(IG, st, True, price="₹74,999 (approx.)")[1] == []
    reasons = scouts.verdict({**IG, "Camera wow": 4, "Multiple talking points": 5}, st, True, price="₹74,999")[1]
    assert reasons == ["over ₹50,000: must meet Multiple talking points, Camera wow"]
    middling = {**{k: 7 for k in IG}, "Orderable in India": 9}  # passes the count rule, not the premium bar
    assert scouts.verdict(middling, st, True, price="₹9,999")[1] == []
    assert scouts.verdict(middling, st, True, price="₹59,999")[1] == ["over ₹50,000: needs a score of 7.5, has 7.0"]


def test_a_yes_no_check_does_not_count_towards_n_criteria():
    st = scouts.clean_settings(scouts.TECHWISER_INSTAGRAM)
    one = {**{k: 2 for k in IG}, "Daily use": 9, "Orderable in India": 9}
    assert scouts.verdict(one, st, True, price="₹999")[1] == ["meets 1 of the 2 criteria needed (Daily use)"]


def test_budget_settings_are_checked():
    st = scouts.clean_settings({**scouts.TECHWISER_INSTAGRAM, "premium_needs": "camera WOW, Nope",
                                "import_sites": "https://www.ubuy.co.in/in, desertcart.in junk", "price_cap": -5})
    assert st["premium_needs"] == ["Camera wow"] and st["import_sites"] == ["www.ubuy.co.in", "desertcart.in"]
    assert st["price_cap"] == 0
    plain = scouts.clean_settings({})
    assert scouts.verdict(HIGH, plain, True)[1] == []  # no budget set: price doesn't matter


def test_evidence_searches_import_sites_and_the_judge_hears_the_budget(db, files):
    st = scouts.clean_settings({**scouts.TECHWISER_INSTAGRAM, "sources": ["youtube", "web"]})
    asked = []
    tools = fake_tools({})
    tools.web = lambda q: asked.append(q) or f"web for {q}"
    text = scouts.evidence_for("Flip Lamp", st, tools)
    assert asked == ["Flip Lamp", "Flip Lamp price India", "Flip Lamp site:ubuy.co.in OR site:desertcart.in"]
    assert "== import sites shipping to India ==" in text
    prompts = []

    def chat(system, user, n):
        prompts.append(system)
        return json.dumps({"scores": IG, "summary": "s", "why": "w", "price": "₹64,999"})

    out = scouts.judge({"name": "Flip Lamp"}, VIDEO, text, st, chat)
    assert out["passed"] and out["price"] == "₹64,999"
    assert "₹1,00,000" in prompts[0] and "ubuy.co.in" in prompts[0] and "₹<amount>" in prompts[0]


def test_recheck_rejudges_only_unreviewed_finds_without_video_lookups(db, files):
    sc = _scout(db)
    with db() as s:
        for i, status in enumerate(("new", "new", "kept")):
            s.add(ScoutFinding(id=f"f{i}", scout_id=sc.id, run_id="r", key=f"k{i}", name=f"Gadget {i}", summary="x",
                               scores=HIGH, total=9, why="", status=status, video_url=VIDEO[0], video_title=VIDEO[1],
                               video_views=5, product_url="", price="", source="", reviewed_by=""))
        s.commit()
        row = s.get(Scout, sc.id)
        row.settings = scouts.clean_settings({**row.settings, **{k: scouts.TECHWISER_INSTAGRAM[k] for k in (
            "criteria", "market", "price_cap", "premium_from", "premium_needs", "import_sites")}})
        s.commit()
        verdicts = {"Gadget 0": ({**IG}, "₹2,999"), "Gadget 1": ({**IG, "Orderable in India": 2}, "")}

        def chat(system, user, n):
            name = next(k for k in verdicts if f"Product: {k} " in user)
            return json.dumps({"scores": verdicts[name][0], "summary": "s", "why": "w", "price": verdicts[name][1]})

        tools = fake_tools({})
        tools.chat = chat
        tools.video = lambda name: pytest.fail("no YouTube lookups on a recheck")
        assert scouts.recheck(s, row, tools) == {"checked": 2, "still_found": 1, "moved_below": 1, "failed": 0}
        got = {f.name: f for f in s.query(ScoutFinding)}
        assert got["Gadget 0"].status == "new" and got["Gadget 0"].price == "₹2,999"
        assert got["Gadget 1"].status == "below" and "Orderable in India" in got["Gadget 1"].why
        assert got["Gadget 2"].status == "kept" and got["Gadget 2"].scores == HIGH


def test_a_find_without_a_video_passes_only_when_it_scores_high():
    st = scouts.clean_settings(scouts.TECHWISER_INSTAGRAM)
    assert st["require_video"] is False and st["no_video_score"] == 8.0
    assert scouts.verdict(IG, st, False, price="₹2,999")[1] == []  # all 9s
    good = {**{k: 7 for k in IG}, "Orderable in India": 9}
    assert scouts.verdict(good, st, True, price="₹2,999")[1] == []
    assert scouts.verdict(good, st, False, price="₹2,999")[1] == ["no reference video: needs a score of 8.0, has 7.0"]
    strict = scouts.clean_settings({**scouts.TECHWISER_INSTAGRAM, "require_video": True})
    assert scouts.verdict(IG, strict, False, price="₹2,999")[1] == ["no reference video found"]


def test_rules_are_learned_from_review_reasons_per_scout_and_relearned_on_new_reviews(db, files):
    a, b = _scout(db), _scout(db)
    asked = []

    def chat(system, user, n):
        asked.append(user)
        return json.dumps(["Avoid lamps: the team has enough of them", "Prefer tools under ₹5,000"])

    with db() as s:
        for i, (status, note) in enumerate((("rejected", "too many lamps already"), ("rejected", ""), ("kept", ""))):
            s.add(ScoutFinding(id=f"a{i}", scout_id=a.id, run_id="r", key=f"k{i}", name=f"Lamp {i}", summary="x",
                               scores={}, total=5, why="", status=status, review_note=note, reviewed_by="r1"))
        s.add(ScoutFinding(id="b0", scout_id=b.id, run_id="r", key="kb", name="Secret B find", summary="x", scores={},
                           total=5, why="", status="rejected", review_note="b's own reason", reviewed_by="r1"))
        s.commit()
        rules = scouts.learned_rules(s, a.id, chat)
        assert rules == "- Avoid lamps: the team has enough of them\n- Prefer tools under ₹5,000"
        assert "REASON: too many lamps already" in asked[0] and "Secret B" not in asked[0]
        assert scouts.learned_rules(s, a.id, chat) == rules and len(asked) == 1  # cached while reviews are the same
        s.get(ScoutFinding, "a1").review_note = "boring"
        s.commit()
        scouts.learned_rules(s, a.id, chat)
        assert len(asked) == 2 and "REASON: boring" in asked[1]
        assert scouts.learned_rules(s, b.id, chat) == ""  # fewer than 3 reviews: nothing to learn yet
    prompts = []
    st = {**scouts.clean_settings(scouts.TECHWISER_INSTAGRAM), "_rules": rules}
    scouts.judge({"name": "X"}, VIDEO, "e", st, lambda sy, u, n: prompts.append(sy) or json.dumps({"scores": IG}))
    assert "Avoid lamps" in prompts[0] and "Avoid lamps" in scouts._brief(st)


def test_india_evidence_includes_marketplace_listings_when_the_scout_has_them():
    st = scouts.clean_settings({**scouts.TECHWISER_INSTAGRAM, "sources": ["web"]})
    tools = fake_tools({})
    tools.shops = lambda q: f"amazon.in listings for {q}"
    text = scouts.evidence_for("Flip Lamp", st, tools)
    assert "== Indian marketplaces (Amazon.in, Flipkart, Reliance Digital) ==\namazon.in listings for Flip Lamp" in text
    assert "marketplaces" not in scouts.evidence_for("Flip Lamp", {**st, "market": "USA"}, tools)
