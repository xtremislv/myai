"""Showing the model only the tool groups a message needs (app/orchestrator/toolsel.py)."""
from __future__ import annotations

import json

import pytest

from app.llm import client as llm
from app.orchestrator import nodes, toolsel
from app.orchestrator.context import tool_groups
from app.orchestrator.tools import tool_schemas, tools_for

ALL = tool_schemas(None)
REAL_CLASSIFY = toolsel.classify  # captured before conftest's per-test stub replaces it


def _names(schemas):
    return [t["function"]["name"] for t in schemas]


def _ungrouped(schemas):
    return [n for n in _names(schemas) if n not in toolsel.GROUP_OF]


@pytest.fixture
def sorted_as(monkeypatch):
    calls = []

    def use(groups):
        def classify(message, recent, available=None):
            calls.append(message)
            return None if groups is None else frozenset(groups)

        monkeypatch.setattr(toolsel, "classify", classify)
        toolsel._cache.clear()
        return calls

    yield use
    toolsel._cache.clear()


def test_only_the_sorted_groups_and_ungrouped_tools_are_shown_in_their_usual_order(sorted_as):
    sorted_as({"mail"})
    shown = toolsel.select(ALL, None, [{"role": "user", "content": "summarize my unread mail"}])
    names = _names(shown)
    assert set(names) == set(toolsel.GROUPS["mail"]) | set(_ungrouped(ALL))
    assert "more_tools" in names and "web_search" not in names
    assert names == [n for n in _names(ALL) if n in names]  # same order: shared cache prefixes


def test_a_greeting_gets_no_groups_but_can_still_ask_for_more(sorted_as):
    sorted_as(set())
    names = _names(toolsel.select(ALL, None, [{"role": "user", "content": "hello"}]))
    assert names == _ungrouped(ALL) and "more_tools" in names


def test_when_sorting_fails_every_tool_is_shown(sorted_as):
    sorted_as(None)
    assert toolsel.select(ALL, None, [{"role": "user", "content": "anything"}]) == ALL


def test_tools_used_in_recent_turns_stay_for_follow_ups(sorted_as):
    sorted_as(set())  # "yes send it" on its own needs nothing...
    messages = [
        {"role": "user", "content": "draft a reply to Rahul"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "name": "read_mail", "args": {}}]},
        {"role": "tool", "content": "mail", "tool_call_id": "1"},
        {"role": "assistant", "content": "Here is the draft. Send it?"},
        {"role": "user", "content": "yes send it"},
    ]
    names = _names(toolsel.select(ALL, None, messages))
    assert "send_mail" in names and "web_search" not in names  # ...but mail was just used


def test_more_tools_adds_the_groups_the_model_asks_for(sorted_as):
    sorted_as(set())
    messages = [
        {"role": "user", "content": "save the plan somewhere"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "1", "type": "function", "function": {"name": "more_tools", "arguments": json.dumps({"groups": "files, reminders"})}}]},
        {"role": "tool", "content": "Added", "tool_call_id": "1"},
    ]
    names = _names(toolsel.select(ALL, None, messages))
    assert "write_file" in names and "set_reminder" in names and "send_mail" not in names


def test_a_turn_is_sorted_once_however_many_tool_rounds_it_takes(sorted_as):
    calls = sorted_as({"web"})
    messages = [{"role": "user", "content": "weather in Pune"}]
    toolsel.select(ALL, None, messages)
    messages += [{"role": "assistant", "content": "", "tool_calls": [{"id": "1", "name": "weather", "args": {}}]},
                 {"role": "tool", "content": "31C", "tool_call_id": "1"}]
    toolsel.select(ALL, None, messages)
    assert calls == ["weather in Pune"]


def test_the_routes_sorting_is_used_only_for_the_message_it_was_made_for(sorted_as):
    calls = sorted_as({"web"})
    token = tool_groups.set((toolsel.message_hash("remind me at 7"), frozenset({"reminders"})))
    try:
        names = _names(toolsel.select(ALL, None, [{"role": "user", "content": "remind me at 7"}]))
        assert "set_reminder" in names and "web_search" not in names and calls == []
        other = _names(toolsel.select(ALL, None, [{"role": "user", "content": "weather?"}]))
        assert "web_search" in other and calls == ["weather?"]  # a stale value is ignored
    finally:
        tool_groups.reset(token)


def test_small_departments_always_get_all_their_tools(sorted_as):
    sorted_as(set())
    few = ALL[:toolsel.MIN_TOOLS_TO_SELECT]
    assert toolsel.select(few, None, [{"role": "user", "content": "hi"}]) == few


def test_a_tool_that_was_not_shown_still_runs_with_its_usual_checks(sorted_as):
    """Selection only changes what the model sees: dispatch and ACL are unchanged."""
    sorted_as(set())
    hidden = "list_reminders"
    assert hidden not in _names(toolsel.select(ALL, None, [{"role": "user", "content": "hi"}]))
    assert hidden in tools_for(None)  # nodes.run_tool dispatches from the full department set


def test_the_classifier_reads_json_and_drops_unknown_groups(monkeypatch):
    replies = iter(['{"groups": ["mail", "rockets"]}', "no json here", '{"groups": "mail"}'])
    monkeypatch.setattr(llm, "chat", lambda *a, **k: next(replies))
    assert REAL_CLASSIFY("check my mail", []) == frozenset({"mail"})
    assert REAL_CLASSIFY("x", []) is None
    assert REAL_CLASSIFY("x", []) is None


def test_more_tools_refuses_unknown_groups_and_passes_the_acl(monkeypatch):
    from app.orchestrator import tools

    asked = []
    monkeypatch.setattr(tools, "_enforce", lambda state, *a: asked.append(a))
    assert tools.more_tools({}, "rockets").startswith("ERROR")
    assert "mail" in tools.more_tools({}, "mail")
    assert asked and asked[0] == ("read", "tools", "list")
    monkeypatch.setattr(tools, "_enforce", lambda state, *a: "ACCESS DENIED: no")
    assert tools.more_tools({}, "mail").startswith("ACCESS DENIED")


def test_the_agent_sends_only_the_selected_tools(sorted_as, monkeypatch):
    sorted_as({"reminders"})
    sent = []

    def chat(messages, **kwargs):
        sent.append(kwargs.get("tools"))
        return "ok"

    monkeypatch.setattr(llm, "chat", chat)
    nodes.agent({"messages": [{"role": "user", "content": "remind me at 7"}], "scratchpad": {}})
    names = _names(sent[-1])
    assert "set_reminder" in names and "send_mail" not in names and len(names) < len(ALL)


def test_more_tools_understands_lists_and_tool_names():
    assert toolsel.requested_groups({"args": {"groups": ["mail", "Files"]}}) == {"mail", "files"}
    assert toolsel.requested_groups({"args": {"groups": "weather"}}) == {"web"}
    assert toolsel.requested_groups({"args": {"groups": "rockets"}}) == set()


def test_the_agent_is_told_to_ask_for_missing_tools_instead_of_improvising():
    for state in ({}, {"scratchpad": {"department": "hr"}}):
        assert "more_tools" in nodes._system_prompt(state)
