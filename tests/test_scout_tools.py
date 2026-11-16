"""Scouts in the chat (app/orchestrator/scout_tools.py): look-ups for members only, settings changes behind an
approver (critical tool + precheck)."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import scout as scouts
from app.db.session import Base, Scout, ScoutFinding, User
from app.departments.packs import critical_tools, needs_go_ahead
from app.departments.registry import seed_departments
from app.orchestrator import scout_tools as T
from app.orchestrator import toolsel
from app.orchestrator.tools import PRECHECKS, TOOLS, tools_for


@pytest.fixture
def db(monkeypatch):
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, expire_on_commit=False)
    with Session() as s:
        seed_departments(s)
        s.add(User(id="r1", username="r1", password_hash="x", roles=["researcher"], prefs={}))
        s.commit()
        ig = scouts.create(s, "researcher", "TechWiser Instagram", "r1", scouts.TECHWISER_INSTAGRAM)
        scouts.create(s, "researcher", "GadgetWiser", "r1", scouts.GADGETWISER)
        rows = [("Flip Lamp", "new", 8.5, "₹2,999", ""), ("Pocket Projector", "kept", 7.9, "₹64,999", "great on camera"),
                ("Dull Cable", "rejected", 3.0, "₹199", "boring, nothing to show"), ("Mui Board", "below", 8.3, "", "")]
        for i, (name, status, total, price, note) in enumerate(rows):
            s.add(ScoutFinding(id=f"f{i}", scout_id=ig.id, run_id="r", key=f"k{i}", name=name, summary=f"{name} does things",
                               scores={"Daily use": 9, "Camera wow": 8}, total=total, status=status, price=price,
                               why="fits" + (" | Not kept: no price in ₹ found" if status == "below" else ""),
                               video_url="https://youtu.be/x" if i < 2 else "", review_note=note, reviewed_by="r1"))
        s.commit()
    monkeypatch.setattr(T, "SessionLocal", Session)
    monkeypatch.setattr(scouts, "save_report", lambda session, scout: "key")
    return Session


def state(dept="researcher", roles=("researcher",), user="r1"):
    return {"subject_id": user, "subject_roles": list(roles), "scratchpad": {"department": dept}, "messages": []}


def test_scout_tools_are_research_tools_and_changes_need_an_approver():
    assert {"scout_info", "scout_findings", "update_scout"} <= set(tools_for("researcher"))
    assert "update_scout" in critical_tools() and needs_go_ahead("update_scout", "researcher")
    assert not needs_go_ahead("scout_findings", "researcher")
    assert PRECHECKS["update_scout"] is T.update_scout_precheck and TOOLS["scout_info"] is T.scout_info
    assert toolsel.GROUP_OF["update_scout"] == "scouts"
    assert "scout_info" not in tools_for(None)  # never in private chat: department data


def test_info_lists_the_departments_scouts_and_shows_one_in_full(db):
    out = T.scout_info(state())
    assert "TechWiser Instagram (active)" in out and "GadgetWiser" in out and "cap ₹1,00,000" in out
    one = T.scout_info(state(), "instagram")
    assert "Orderable in India (weight 0, minimum score 7.0, required)" in one
    assert "over ₹50,000 needs score 7.5+ and Multiple talking points, Camera wow" in one
    assert "without one a find needs a score of 8.0" in one and "ubuy.co.in" in one


def test_findings_filter_sort_and_show_review_reasons(db):
    out = T.scout_findings(state(), "TechWiser Instagram")
    assert out.index("Flip Lamp") < out.index("Pocket Projector") and "Dull Cable" not in out
    assert "Team's reason: great on camera" in out and "UNTRUSTED CONTENT" in out
    rej = T.scout_findings(state(), "instagram", status="rejected")
    assert "Dull Cable" in rej and "boring, nothing to show" in rej
    assert "no price in ₹ found" in T.scout_findings(state(), "instagram", status="below")
    cheap = T.scout_findings(state(), "instagram", status="all", max_price=5000, sort="price_low")
    assert cheap.index("Dull Cable") < cheap.index("Flip Lamp") and "Pocket" not in cheap and "Mui" not in cheap
    assert "No found findings" in T.scout_findings(state(), "instagram", query="toaster")


def test_only_members_of_the_working_department_reach_its_scouts(db):
    assert T.scout_info(state(dept="hr", roles=("hr",))).startswith("No scouts here")  # HR's own: none
    # a researcher working in HR sees HR's scouts, never Research's
    assert "Instagram" not in T.scout_info(state(dept="hr", roles=("hr", "researcher")))
    assert T.scout_info(state(dept="researcher", roles=("hr",))).startswith("ACCESS DENIED")
    assert T.scout_info(state(dept="admin", roles=("admin",))).startswith("ACCESS DENIED")
    assert T.scout_info({"subject_id": "r1", "subject_roles": ["researcher"], "scratchpad": {}}).startswith("ERROR")
    assert "Flip Lamp" not in T.scout_findings(state(dept="hr", roles=("hr",)), "instagram")
    assert T.update_scout_precheck(state(dept="researcher", roles=("hr",)), "instagram", '{"price_cap": 5}').startswith("ACCESS")


def test_a_bad_change_is_refused_before_anyone_is_asked(db):
    pre = T.update_scout_precheck
    assert pre(state(), "Instagram", "not json").startswith("ERROR: changes must be a JSON object")
    assert "unknown setting(s): colour" in pre(state(), "Instagram", '{"colour": "red"}')
    assert "no criterion called 'Smell'" in pre(state(), "Instagram", '{"criteria": {"Smell": null}}')
    assert "needs a description" in pre(state(), "Instagram", '{"criteria": {"Eco": {"weight": 2}}}')
    assert "nothing would change" in pre(state(), "Instagram", '{"price_cap": 100000}')
    assert "which one?" in pre(state(), "wiser", '{"price_cap": 5}') or "no scout" in pre(state(), "wiser", '{"price_cap": 5}')
    assert pre(state(), "Instagram", '{"price_cap": 80000}') is None
    with db() as s:  # the precheck changed nothing
        assert scouts.clean_settings(s.query(Scout).filter_by(name="TechWiser Instagram").one().settings)["price_cap"] == 100000


def test_an_approved_change_is_applied_and_resorts_the_finds(db):
    changes = {"price_cap": 60000, "criteria": {"Camera wow": {"min": 8.5}, "Cool factor": None,
               "Eco friendly": {"description": "Recyclable or low power", "weight": 1}}, "add_ideas": ["magnetic gadget 2"],
               "enabled": False}
    out = T.update_scout(state(), "TechWiser Instagram", json.dumps(changes), "tighter budget")
    assert "price_cap: ₹1,00,000 -> ₹60,000" in out and "criterion removed: Cool factor" in out
    assert "criterion Camera wow, min: 7.0 -> 8.5" in out and "criterion added: Eco friendly" in out
    assert "search ideas added: magnetic gadget 2" in out and "pause the scout" in out
    with db() as s:
        row = s.query(Scout).filter_by(name="TechWiser Instagram").one()
        st = scouts.clean_settings(row.settings)
        assert st["price_cap"] == 60000 and not row.enabled and row.updated_by == "r1"
        assert [c["name"] for c in st["criteria"]][-1] == "Eco friendly" and "Cool factor" not in str(st["criteria"])
        kept = s.get(ScoutFinding, "f1")
        assert kept.status == "kept"  # a person's decision is never undone by a rule change
        gw = scouts.clean_settings(s.query(Scout).filter_by(name="GadgetWiser").one().settings)
        assert gw["price_cap"] == 0  # the other scout is untouched
