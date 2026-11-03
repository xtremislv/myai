"""Deciding what is forwarded to accounts, and ACL gates on the remaining tools."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.acl.engine import ACLEngine
from app.acl.models import Action, Resource, Subject
from app.db.session import Base
from app.departments.registry import seed_departments
from app.llm import client as llm
from app.orchestrator import tools
from app.request_manager import guards
from app.request_manager.guards import (
    forward_verdict,  # the real one; conftest patches the attribute
)


# ---- model verdict ----
@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ('{"category": "order"}', "order"),
        ('{"category": "service"}', "service"),
        ('{"category": "bill"}', "bill"),
        ('{"category": "none"}', "none"),
        ('```json\n{"category": "bill"}\n```', "bill"),
        ('{"category": "explicit"}', None),
        ("no idea", None),
    ],
)
def test_forward_verdict_parsing(monkeypatch, reply, expected):
    monkeypatch.setattr(llm, "chat", lambda messages, **k: reply)
    assert forward_verdict("anything") == expected


def test_forward_verdict_is_none_when_the_model_is_down(monkeypatch):
    def _down(messages, **k):
        raise ConnectionError("vLLM is down")

    monkeypatch.setattr(llm, "chat", _down)
    assert forward_verdict("anything") is None


def test_model_decides_when_available(monkeypatch):
    monkeypatch.setattr(guards, "forward_verdict", lambda m: "none")
    assert guards.detect_forward_intent("how do we usually place an order?") is None

    monkeypatch.setattr(guards, "forward_verdict", lambda m: "order")
    assert guards.detect_forward_intent("get us five laptops from Dell") == "order"


# ---- keyword fallback, used only when the model is unavailable ----
@pytest.mark.parametrize(
    "message",
    [
        "In order to compare Q3 reports, summarize them",
        "Revenue reached 2 billion last year",
        "Please notebook this",
        "our billing team moved floors",
        "who reordered the shelves",
    ],
)
def test_fallback_does_not_forward_harmless_wording(message):
    assert guards.detect_forward_intent(message) is None


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("please place an order for 10 laptops", "order"),
        ("order 10 laptops", "order"),
        ("buy a new router", "order"),
        ("book a courier for Monday", "service"),
        ("pay the invoice from the vendor", "bill"),
        ("this bill is overdue", "bill"),
    ],
)
def test_fallback_still_forwards_real_requests(message, kind):
    assert guards.detect_forward_intent(message) == kind


# ---- ACL on the tools that had none ----
@pytest.fixture
def real_acl(monkeypatch):
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as s:
        seed_departments(s)

    def _enforce(state, action, rtype, rid):
        with Session() as s:
            subject = Subject(state["subject_id"], tuple(state["subject_roles"]))
            decision = ACLEngine(s).check(subject, Action(action), Resource(rtype, rid))
            return None if decision.allowed else f"ACCESS DENIED: {decision.reason}"

    monkeypatch.setattr(tools, "_enforce", _enforce)
    monkeypatch.setattr(tools, "SessionLocal", Session)


CALLS = [
    (tools.send_notification, ("warehouse", "hello"), "logistics"),
    (tools.summarize, ("some text",), "researcher"),
]


def test_everyone_may_set_their_own_reminders(real_acl):
    for roles in ((), ("hr",), ("logistics",), ("admin",)):
        assert tools.set_reminder(_state(*roles), "tomorrow 9am", "check stock").startswith("Reminder ")


def _state(*roles):
    return {"subject_id": "u", "subject_roles": list(roles)}


@pytest.mark.parametrize(("tool", "args", "department"), CALLS)
def test_tool_works_for_its_department_and_admin(real_acl, tool, args, department):
    assert not tool(_state(department), *args).startswith("ACCESS DENIED")
    assert not tool(_state("admin"), *args).startswith("ACCESS DENIED")


@pytest.mark.parametrize(("tool", "args", "department"), CALLS)
def test_tool_is_denied_to_other_departments(real_acl, tool, args, department):
    other = "hr"
    assert tool(_state(other), *args).startswith("ACCESS DENIED")


def test_everyone_may_search_the_web_but_not_commit(real_acl, monkeypatch):
    """Read-only web access is global (private chat included); committing is not."""
    from app.browsing import search

    monkeypatch.setattr(search, "web", lambda query, limit=8, api=None: [search.Result("T", "https://x.example", "s")])
    monkeypatch.setattr(tools, "_search_api", lambda: None)
    for roles in ((), ("hr",), ("logistics",)):
        assert "x.example" in tools.web_search(_state(*roles), "market trends")
    assert tools.browser_commit(_state("researcher"), 1, "Book", "x").startswith("ACCESS DENIED")
    assert tools.browser_commit(_state(), 1, "Book", "x").startswith("ACCESS DENIED")


def test_every_tool_has_an_acl_gate():
    """Guards the project rule that no tool skips the ACL."""
    import inspect

    # Shared helpers that run the ACL check for the tools that call them.
    gates = ("_enforce(", "_subject(", "_mailbox(", "_browse(", "_my_files(", "_dept_files(", "_save(",
             "_resolve_template(")
    # Company templates: every lookup goes through the store, which asks the ACL engine per template.
    from app.documents import store

    assert "store.find(" in inspect.getsource(tools._resolve_template)
    assert "_my_files(" in inspect.getsource(tools._resolve_template)
    assert "ACLEngine(" in inspect.getsource(store.visible) and "enforce(" in inspect.getsource(store.save)
    for helper in (tools._mailbox, tools._browse, tools._my_files):
        assert "_enforce(" in inspect.getsource(helper)
    # Department files go through DeptFiles, which asks the ACL engine before every operation.
    assert "ACLEngine(" in inspect.getsource(tools._dept_files)
    assert "_my_files(" in inspect.getsource(tools._save) and "_dept_files(" in inspect.getsource(tools._save)
    for name, fn in tools.TOOLS.items():
        source = inspect.getsource(fn)
        assert any(g in source for g in gates), f"{name} has no ACL gate"
