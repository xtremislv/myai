"""Per-department tool registries (Phase 3): an agent only sees/dispatches its
department's tools."""
from __future__ import annotations

from app.departments.packs import PRIVATE_TOOLS
from app.orchestrator.tools import DEPARTMENT_TOOLS, TOOLS, tools_for


def test_each_department_tool_exists_in_table():
    for dept, names in DEPARTMENT_TOOLS.items():
        if names == ("*",):
            continue
        for n in names:
            assert n in TOOLS, f"{dept} references unknown tool {n}"


def test_scoped_subsets():
    assert set(tools_for("logistics")) == set(DEPARTMENT_TOOLS["logistics"])
    assert "inventory_adjust" in tools_for("logistics")
    # account cannot adjust inventory (not in its toolset)
    assert "inventory_adjust" not in tools_for("account")
    # researcher has no finance/mail tools
    assert "read_mail" not in tools_for("researcher")


def test_admin_and_superuser():
    assert tools_for("admin") == TOOLS            # admin = all tools
    assert "inventory_adjust" in tools_for("superuser")


def test_unknown_department_has_no_tools():
    assert tools_for("nope") == {}
    # Private chat: the person's own things and the web; never company systems.
    private = tools_for(None)
    assert set(private) == set(PRIVATE_TOOLS)
    assert not {"get_secret", "balance_lookup", "initiate_payment", "manage_order", "read_document"} & set(private)
