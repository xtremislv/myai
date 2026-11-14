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
from app.db.session import Base, Notification, Scout, ScoutFinding, ScoutRun, User, get_session
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
    assert st["criteria"] == [{"name": "Fun", "description": "", "weight": 3, "min": 0.0}] and st["ideas"] == ["a", "b"]
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
