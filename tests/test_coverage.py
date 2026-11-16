"""Our coverage and competitors' hits (app/coverage.py) and how a scout round uses them (app/scout.py)."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import coverage
from app import scout as scouts
from app.browsing import youtube
from app.db.session import Base, ChannelSync, ChannelVideo, ScoutFinding, ScoutRun

NOW = dt.datetime.now(dt.timezone.utc)


@pytest.fixture
def db():
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, expire_on_commit=False)


def _v(i, title, views=0, days=10, channel="UCours", description=""):
    return ChannelVideo(id=f"vid{i:08d}", channel_id=channel, title=title, description=description, views=views,
                        published_at=NOW - dt.timedelta(days=days))


def test_sync_reads_new_uploads_until_a_known_one_and_refreshes_views_weekly(db, monkeypatch):
    pages = {"": (["n2", "n1", "o1"], "p2"), "p2": (["o0"], "")}
    fetched = []
    monkeypatch.setattr(youtube, "channels", lambda ids, key: [youtube.Channel(
        id="UCx", title="Rival", handle="@rival", subscribers=1, views=1, videos=4, created="", country="",
        description="", uploads="UUx")])
    monkeypatch.setattr(youtube, "upload_page", lambda pl, key, page="": pages[page])

    def videos(ids, key):
        fetched.append(list(ids))
        return [youtube.Video(id=i, title=f"T {i}", channel="Rival", published="2026-01-02", views=5) for i in ids]

    monkeypatch.setattr(youtube, "_videos", videos)
    with db() as s:
        s.add(ChannelVideo(id="o1", channel_id="UCx", title="old", description="", views=1, published_at=NOW))
        s.add(ChannelSync(channel_id="UCx", title="", refs=[], videos=1, stats_at=NOW))
        s.commit()
        assert coverage.sync(s, "UCx", "k") == 2
        assert fetched == [["n2", "n1"]]  # stopped at the known upload; views fresh: not re-read
        assert coverage.sync(s, "UCx", "k") == 0 and len(fetched) == 1  # synced today already
        s.get(ChannelSync, "UCx").stats_at = NOW - dt.timedelta(days=8)
        s.commit()
        coverage.sync(s, "UCx", "k", force=True)
        assert sorted(fetched[-1]) == ["n1", "n2", "o1"]  # a week on: everyone's views re-read
        assert s.get(ChannelSync, "UCx").title == "Rival" and s.get(ChannelVideo, "n1").views == 5


def test_matching_uses_distinctive_words_and_hits_need_views_and_age(db):
    with db() as s:
        s.add_all([_v(1, "This lamp FLOATS! Leviosa levitating lamp review", 9000),
                   _v(2, "5 cool gadgets under 500", 50000, description="1. Sharge Disk Pro 2 power bank ..."),
                   _v(3, "Best desk lamp", 100),
                   _v(4, "Old viral hit", 3_000_000, days=400, channel="UCrival"),
                   _v(5, "New viral hit", 3_000_000, days=30, channel="UCrival"),
                   _v(6, "Old small video", 20_000, days=400, channel="UCrival")])
        s.commit()
        idx = coverage.index(s, ["UCours"])
        assert [v.title for v in coverage.covered_matches(idx, "Leviosa Rotating Suspension Lamp")] == [
            "This lamp FLOATS! Leviosa levitating lamp review"]
        assert [v.id for v in coverage.covered_matches(idx, "SHARGE Disk Pro 2")] == ["vid00000002"]  # description
        assert coverage.covered_matches(idx, "Cool gadget mini") == []  # only generic words: never a match
        assert [v.title for v in coverage.hits(s, ["UCrival"], 1_000_000, 180)] == ["Old viral hit"]


def test_settings_take_channel_lists():
    st = scouts.clean_settings({"competitor_channels": "@rivaltech\nhttps://youtube.com/@other, @rivaltech",
                                "own_channels": "ours", "hit_views": "2000000"})
    assert st["competitor_channels"] == ["@rivaltech", "https://youtube.com/@other"] and st["own_channels"] == ["ours"]
    assert st["hit_views"] == 2_000_000 and st["skip_covered"] is True and st["hit_age_days"] == 180


# ---- in a round ----
HIGH = {c["name"]: 9 for c in scouts.DEFAULTS["criteria"]}
VIDEO = ("https://www.youtube.com/watch?v=aaaaaaaaaaa", "Lamp video", 2_500_000, "")
HIT = {"id": "hit00000001", "title": "This $20 gadget changed my desk", "description": "the Magic Cube charger",
       "views": 2_400_000, "channel": "Rival Tech", "url": "https://www.youtube.com/watch?v=hit00000001",
       "published": "2025-01-01"}


def tools_for(products, covered=None, hits=(), prompts=None):
    prompts = prompts if prompts is not None else []

    def chat(system, user, n):
        prompts.append((system, user))
        if system.startswith("You plan searches"):
            return "[]"
        if system.startswith("A competitor's video"):
            return json.dumps({"product": "Magic Cube charger", "searches": ["better cube charger"]})
        if system.startswith("From search results"):
            if hits and "better cube charger" not in user:
                return "[]"  # with a hit, products turn up only for the hit's search
            return json.dumps([{"name": p, "what": "a gadget"} for p in products])
        if system.startswith("We run YouTube channels"):
            return json.dumps([i for i, line in enumerate([ln for ln in user.splitlines() if ln[:1].isdigit()], 1)
                               if any(c in line for c in (covered or {}))])
        if system.startswith("You pick which products"):
            return json.dumps(list(range(1, len(products) + 1)))
        if system.startswith("You judge one product"):
            return json.dumps({"scores": HIGH, "summary": "better than the cube", "why": "w", "price": "₹999"})
        raise AssertionError(system[:50])

    return scouts.Tools(
        youtube_page=lambda q: q, reddit=lambda q: q, stores=lambda q: q, web=lambda q: q,
        video=lambda name: VIDEO, chat=chat,
        covered=lambda refs, names: {n: [{"title": v, "url": "https://www.youtube.com/watch?v=ours0000001",
                                          "description": ""}] for n, v in (covered or {}).items() if n in names},
        hits=lambda refs, views, age: list(hits))


def _scout(db, **settings):
    with db() as s:
        return scouts.create(s, "researcher", "IG", "r1", {"sources": ["web"], "ideas_per_round": 1, "fresh_ideas": 0,
                                                           **settings})


def test_a_product_we_already_covered_is_skipped_without_spending_a_lookup(db):
    sc = _scout(db)
    tools = tools_for(["Leviosa Lamp", "Flip Fan"], covered={"Leviosa Lamp": "Leviosa lamp review (ours)"})
    with db() as s:
        row = s.get(scouts.Scout, sc.id)
        run = scouts.run_round(s, row, tools)
        by = {f.name: f for f in s.query(ScoutFinding)}
        assert by["Leviosa Lamp"].status == "below" and "already covered by us" in by["Leviosa Lamp"].why
        assert by["Leviosa Lamp"].source.startswith(scouts.COVERED) and by["Flip Fan"].status == "new"
        assert run.judged == 1 and "skipped 1 we already covered: Leviosa Lamp" in run.notes
        assert scouts._judged_today(s, sc.id) == 1  # the skip used no video lookup


def test_an_earlier_find_we_have_since_covered_leaves_the_found_list(db):
    sc = _scout(db)
    with db() as s:
        scouts.run_round(s, s.get(scouts.Scout, sc.id), tools_for(["Flip Fan"]))
        s.query(ScoutFinding).filter_by(name="Flip Fan").one().status = "new"
        s.commit()
        moved = scouts.drop_covered_finds(s, s.get(scouts.Scout, sc.id), tools_for([], covered={"Flip Fan": "We tried the Flip Fan"}))
        f = s.query(ScoutFinding).filter_by(name="Flip Fan").one()
        assert moved == 1 and f.status == "below" and "We tried the Flip Fan" in f.why


def test_competitor_hits_become_searches_for_better_products(db):
    sc = _scout(db, competitor_channels=["@rivaltech"])
    prompts = []
    with db() as s:
        run = scouts.run_round(s, s.get(scouts.Scout, sc.id), tools_for(["Cube Charger X"], hits=[HIT], prompts=prompts))
        assert "better cube charger" in run.queries and "ideas from Rival Tech's" in run.notes
        f = s.query(ScoutFinding).one()
        assert f.source.startswith(scouts.HIT) and "2.4M views" in f.source
        assert scouts.inspired_by(f.source) == {"label": "Rival Tech 'This $20 gadget changed my desk' (2.4M views): Magic Cube charger",
                                                 "url": "https://www.youtube.com/watch?v=hit00000001"}
        judge = next(sy for sy, _ in prompts if sy.startswith("You judge one product"))
        assert "better than, or similar to, 'Magic Cube charger'" in judge


def test_hits_are_taken_in_turn_and_none_without_competitors(db):
    sc = _scout(db, competitor_channels=["@rivaltech"], hits_per_round=1)
    hits = [{**HIT, "id": f"h{i}", "title": f"hit {i}"} for i in range(3)]
    with db() as s:
        row = s.get(scouts.Scout, sc.id)
        st = scouts.clean_settings(row.settings)
        tools = tools_for([], hits=hits)
        picked = []
        for _ in range(4):
            picked.append(scouts._hits_for_round(s, row, st, tools)[0]["title"])
            s.add(ScoutRun(id=f"r{len(picked)}", scout_id=row.id, trigger="t", status="done", queries=[],
                           candidates=0, judged=0, added=0, notes=""))
            s.commit()
        assert picked == ["hit 0", "hit 1", "hit 2", "hit 0"]
        assert len(scouts._hits_for_round(s, row, st, tools)) == 3  # spares for hits without a fitting product
        assert scouts._hits_for_round(s, row, {**st, "competitor_channels": []}, tools) == []
