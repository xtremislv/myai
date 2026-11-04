"""Department packs + data-access schema: seeding, role scopes, owner-based
access, inventory rules, and admin exclusions. In-memory SQLite."""
from __future__ import annotations

import base64
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.acl.engine import ACLEngine
from app.acl.models import Action, Resource, Subject
from app.db.session import Base, Department
from app.departments.packs import DEFAULT_DEPARTMENTS
from app.departments.registry import get_department, seed_departments


@pytest.fixture
def eng(monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", base64.b64encode(os.urandom(32)).decode())
    from app import config

    config.get_settings.cache_clear()
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    s = Session()
    seed_departments(s)
    yield ACLEngine(s)
    s.close()


def _s(*roles, uid="u1"):
    return Subject(uid, tuple(roles))


def allow(eng, subj, action, rtype, rid):
    return eng.check(subj, action, Resource(rtype, rid)).allowed


def test_seeded_departments(eng):
    assert eng._session.query(Department).count() == len(DEFAULT_DEPARTMENTS)
    assert get_department(eng._session, "account").requires_approval is True


def test_account_scope(eng):
    a = _s("account")
    assert allow(eng, a, Action.READ, "accounts", "ledger")
    assert allow(eng, a, Action.WRITE, "balance_sheet", "2026")
    assert allow(eng, a, Action.READ, "mail", "finance/inbox")
    assert not allow(eng, a, Action.READ, "mail", "career/inbox")
    assert not allow(eng, a, Action.WRITE, "inventory", "sku1")   # account reads inventory only


def test_balance_sheet_and_accounts_restricted(eng):
    other = _s("logistics")
    assert not allow(eng, other, Action.READ, "accounts", "ledger")
    assert not allow(eng, other, Action.READ, "balance_sheet", "2026")


def test_personal_info_owner_only(eng):
    owner = _s(uid="alice")
    intruder = _s("admin", uid="bob")
    assert allow(eng, owner, Action.READ, "personal_info", "alice/profile")
    assert not allow(eng, owner, Action.READ, "personal_info", "carol/profile")
    # even admin cannot read someone's personal info
    assert not allow(eng, intruder, Action.READ, "personal_info", "alice/profile")


def test_personal_mail_and_chat_history_owner_only(eng):
    alice = _s(uid="alice")
    assert allow(eng, alice, Action.READ, "mail", "personal/alice/m1")
    assert not allow(eng, alice, Action.READ, "mail", "personal/bob/m1")
    assert allow(eng, alice, Action.READ, "chat_history", "alice/t1")
    assert not allow(eng, alice, Action.READ, "chat_history", "bob/t1")


def test_mail_categories(eng):
    assert allow(eng, _s("hr"), Action.READ, "mail", "career/inbox")
    assert allow(eng, _s("admin"), Action.READ, "mail", "contact/inbox")
    assert not allow(eng, _s("hr"), Action.READ, "mail", "finance/inbox")


def test_inventory_rules(eng):
    # anyone may READ inventory
    assert allow(eng, _s("researcher"), Action.READ, "inventory", "sku1")
    assert allow(eng, _s("hr"), Action.LIST, "inventory", "sku1")
    # only logistics + superuser may WRITE
    assert allow(eng, _s("logistics"), Action.WRITE, "inventory", "sku1")
    assert allow(eng, _s("superuser"), Action.WRITE, "inventory", "sku1")
    assert not allow(eng, _s("researcher"), Action.WRITE, "inventory", "sku1")
    assert not allow(eng, _s("hr"), Action.WRITE, "inventory", "sku1")


def test_admin_access_and_exclusions(eng):
    adm = _s("admin", uid="root")
    # The admin's own: the contact mailbox, admin folders, company templates, reports, platform.
    assert allow(eng, adm, Action.READ, "mail", "contact/inbox")
    assert allow(eng, adm, Action.WRITE, "shared", "admin/notes.md")
    assert allow(eng, adm, Action.WRITE, "template", "global/Letterhead")
    assert allow(eng, adm, Action.READ, "report", "nsfw/r1")
    assert allow(eng, adm, Action.READ, "inventory", "sku1")
    # Other departments' official mail and documents are for their members only (2026-11-04).
    for rtype, rid in (("accounts", "ledger"), ("balance_sheet", "q3"), ("order", "o1"), ("payment", "p1"),
                       ("mail", "finance/inbox"), ("mail", "career/inbox"), ("shared", "account/q3.xlsx"),
                       ("shared", "hr/offers.docx"), ("template", "hr/Offer letter"), ("research", "notes"),
                       ("shipments", "s1"), ("secret", "mail/finance")):
        assert not allow(eng, adm, Action.READ, rtype, rid), (rtype, rid)
    assert not allow(eng, adm, Action.WRITE, "inventory", "sku1")
    # Being admin as well doesn't take a member's access away (no deny on department data).
    both = Subject(id="root", roles=("admin", "account"))
    assert allow(eng, both, Action.READ, "mail", "finance/inbox")
    assert allow(eng, both, Action.READ, "accounts", "ledger")
    # The superuser department has no way into Accounts or HR either.
    sup = _s("superuser")
    assert not allow(eng, sup, Action.READ, "mail", "finance/inbox")
    assert not allow(eng, sup, Action.READ, "shared", "account/q3.xlsx")
    assert not allow(eng, sup, Action.READ, "mail", "contact/inbox")
    assert not allow(eng, sup, Action.READ, "mail", "career/inbox")
    assert not allow(eng, sup, Action.READ, "shared", "hr/offers.docx")
    assert not allow(eng, sup, Action.READ, "template", "hr/Offer letter")
    assert not allow(eng, sup, Action.READ, "report", "nsfw/r1")
    # but NOT chat history or personal data (incl. personal mail)
    assert not allow(eng, adm, Action.READ, "chat_history", "someone/t1")
    assert not allow(eng, adm, Action.READ, "personal_info", "someone/p")
    assert not allow(eng, adm, Action.READ, "mail", "personal/someone/m1")


def test_unknown_role_denied(eng):
    nobody = _s("intern")
    assert not allow(eng, nobody, Action.READ, "accounts", "ledger")
    # but still gets the global inventory read
    assert allow(eng, nobody, Action.READ, "inventory", "sku1")


# ---- documents follow the data schema ----
def test_document_keys_map_to_the_department_data_types():
    from app.storage.objects import resource_for

    def r(key, **kw):
        res = resource_for(key, **kw)
        return f"{res.type}:{res.id}"

    assert r("research/q3.txt") == "research:q3.txt"
    assert r("research/", listing=True) == "research:*"
    assert r("personal_info/alice/cv.pdf") == "personal_info:alice/cv.pdf"
    assert r("loose.txt") == "object:loose.txt"
    assert r("", listing=True) == "object:*"


def test_researcher_can_read_research_documents_but_not_accounts(eng):
    from app.storage.objects import resource_for

    me = Subject("rae", ("researcher",))
    assert eng.check(me, Action.READ, resource_for("research/q3.txt")).allowed
    assert eng.check(me, Action.LIST, resource_for("reports/", listing=True)).allowed
    assert not eng.check(me, Action.READ, resource_for("accounts/ledger.csv")).allowed
    assert not eng.check(me, Action.LIST, resource_for("", listing=True)).allowed


def test_reseeding_removes_built_in_policies_that_were_dropped_but_keeps_hand_made_ones():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db.session import ACLPolicy, Base
    from app.departments.registry import _policy_id, seed_departments

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        old = _policy_id("admin", "allow", "*", "*")  # the admin's old blanket grant
        s.add(ACLPolicy(id=old, subject="admin", effect="allow", action="*", resource="*"))
        s.add(ACLPolicy(id="a" * 32, subject="admin", effect="allow", action="read", resource="inventory:x"))
        s.commit()
        assert seed_departments(s)["policies_removed"] == 1
        assert s.get(ACLPolicy, old) is None and s.get(ACLPolicy, "a" * 32) is not None
        assert seed_departments(s)["policies_removed"] == 0
