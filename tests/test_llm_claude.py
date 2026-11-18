"""LLM_PROVIDER=anthropic: app/llm/anthropic_backend.py converts the app's OpenAI-shaped calls for Claude."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.llm import anthropic_backend as claude
from app.llm import client
from app.orchestrator.nodes import assistant_calls


@pytest.fixture
def anthropic(monkeypatch):
    for s in {id(x): x for x in (client._settings, claude.get_settings())}.values():
        monkeypatch.setattr(s, "llm_provider", "anthropic")
        monkeypatch.setattr(s, "llm_model", "claude-sonnet-5-5")
        monkeypatch.setattr(s, "llm_smart_routing", True)
        monkeypatch.setattr(s, "llm_standard_model", "")
        monkeypatch.setattr(s, "llm_thinking", "off")
        monkeypatch.setattr(s, "llm_effort", "")
        monkeypatch.setattr(s, "llm_context_tokens", 200_000)
    fake = FakeAPI()
    monkeypatch.setattr(claude, "_client", fake)
    from app.llm import model_manager

    monkeypatch.setattr(model_manager, "ledger", model_manager.Ledger(redis_client=False))
    return fake


def _block(**kw):
    return SimpleNamespace(**kw)


def _message(text="", calls=(), stop="end_turn"):
    content = [_block(type="thinking", thinking="", signature="sig")]
    if text:
        content.append(_block(type="text", text=text))
    content += [_block(type="tool_use", id=i, name=n, input=a) for i, n, a in calls]
    usage = _block(input_tokens=100, cache_read_input_tokens=900, cache_creation_input_tokens=0, output_tokens=20)
    return _block(content=content, stop_reason=stop, usage=usage)


class FakeStream:
    def __init__(self, message, pieces):
        self.message, self.pieces = message, pieces

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter([_block(type="text", text=p) for p in self.pieces])

    def get_final_message(self):
        return self.message


class FakeAPI:
    def __init__(self):
        self.requests = []
        self.reply = _message("Hello there")
        self.messages = self

    def with_options(self, **kw):
        return self

    def create(self, **request):
        self.requests.append(request)
        return self.reply

    def stream(self, **request):
        self.requests.append(request)
        return FakeStream(self.reply, ["Hello", " there"])


def test_tool_turns_are_converted_and_every_call_gets_a_result():
    calls = [{"id": "call_1", "name": "web_search", "args": {"q": "x"}},
             {"id": "call:2", "name": "scrape_page", "args": "not json"}]
    messages = [
        {"role": "system", "content": "Be useful."},
        {"role": "user", "content": "find x"},
        assistant_calls("Looking.", calls),
        {"role": "tool", "tool_call_id": "call_1", "name": "web_search", "content": "results"},
        # call:2 has no stored result (declined approval), then an orphan result whose call was trimmed
        {"role": "tool", "tool_call_id": "gone", "content": "old"},
        {"role": "system", "content": "Wrap up."},
    ]
    system, turns = claude.convert_messages(messages)
    assert system == "Be useful."
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    tool_uses = [b for b in turns[1]["content"] if b["type"] == "tool_use"]
    assert [b["id"] for b in tool_uses] == ["call_1", "call_2"]
    assert tool_uses[1]["input"] == {"raw_arguments": "not json"}
    last = turns[2]["content"]
    assert [b["type"] for b in last] == ["tool_result", "tool_result", "text", "text"]  # results first
    assert {b["tool_use_id"] for b in last[:2]} == {"call_1", "call_2"}
    assert last[1]["content"] == claude.NO_RESULT or last[0]["content"] == claude.NO_RESULT
    assert "Tool result:\nold" in last[2]["text"] and last[3]["text"].startswith("[System note]")


def test_conversation_starts_and_ends_on_a_user_turn():
    _, turns = claude.convert_messages([{"role": "assistant", "content": "Hi"}])
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]


def test_chat_returns_text_and_calls_and_records_claude_usage(anthropic, monkeypatch):
    seen = {}
    monkeypatch.setattr(client, "_record_usage", lambda m, u, t: seen.update(model=m, usage=u, task=t))
    anthropic.reply = _message("", [("toolu_1", "web_search", {"q": "phones"})], stop="tool_use")
    tools = [{"type": "function", "function": {"name": "web_search", "description": "Search",
                                               "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}]
    reply = client.chat([{"role": "user", "content": "phones?"}], tools=tools, temperature=0)
    assert reply == "" and reply.tool_calls == [{"id": "toolu_1", "name": "web_search", "args": {"q": "phones"}}]
    req = anthropic.requests[-1]
    assert req["model"] == "claude-sonnet-5-5" and "temperature" not in req
    assert req["tools"][0]["input_schema"]["properties"]["q"] == {"type": "string"}
    assert req["tool_choice"] == {"type": "auto"} and req["thinking"] == {"type": "between_tools"}
    assert req["cache_control"] == {"type": "ephemeral"}
    assert seen["usage"].prompt_tokens == 1000 and seen["usage"].prompt_tokens_details.cached_tokens == 900


def test_streamed_words_reach_the_sink(anthropic):
    words = []
    token = client.token_sink.set(words.append)
    try:
        reply = client.chat([{"role": "user", "content": "hi"}], stream=True)
    finally:
        client.token_sink.reset(token)
    assert words == ["Hello", " there"] and reply == "Hello there"


def test_thinking_settings_per_model(anthropic):
    assert claude.thinking_params("claude-haiku-5-5") == {"thinking": {"type": "disabled"}}
    assert claude.thinking_params("claude-opus-5-5") == {}  # always thinks: nothing to send
    assert claude.thinking_params("claude-fable-5-2") == {}
    req = claude.params([{"role": "user", "content": "x"}], model="claude-opus-5-5", max_tokens=120,
                        tools=None, tool_choice=None)
    assert req["max_tokens"] == claude.THINKING_MIN_TOKENS  # room to think before a short verdict


def test_triage_goes_to_haiku_and_is_costed(anthropic):
    from app.llm import model_manager

    client.chat([{"role": "user", "content": "hi"}], task="request_manager.guards.triage")
    assert anthropic.requests[-1]["model"] == "claude-haiku-5-5"
    summary = model_manager.month_summary()
    assert summary["calls"]["light"] == 1 and summary["cost_usd"]["light"] > 0


def test_tool_choice_none_and_adaptive_thinking(anthropic, monkeypatch):
    monkeypatch.setattr(claude.get_settings(), "llm_thinking", "adaptive")
    monkeypatch.setattr(claude.get_settings(), "llm_effort", "low")
    req = claude.params([{"role": "user", "content": "x"}], model="claude-sonnet-5-5", max_tokens=500,
                        tools=[{"type": "function", "function": {"name": "a", "parameters": {}}}], tool_choice="none")
    assert req["tool_choice"] == {"type": "none"} and req["thinking"] == {"type": "adaptive"}
    assert req["output_config"] == {"effort": "low"} and req["max_tokens"] == claude.THINKING_MIN_TOKENS


def test_api_connections_stay_warm_and_carry_parallel_calls():
    from app.llm import anthropic_backend

    http = anthropic_backend._http_client()
    pool = http._transport._pool
    assert pool._keepalive_expiry == anthropic_backend.KEEPALIVE_SECONDS >= 60
    assert pool._http2  # triage and tool picking share one connection
    http.close()


def test_cache_breakpoints_on_the_system_prompt_and_before_the_clock_stamp():
    from app.orchestrator import clock

    history = [{"role": "system", "content": clock.with_now("You are TW-ai.")},
               {"role": "user", "content": "first question"},
               {"role": "assistant", "content": "first answer"},
               {"role": "user", "content": "second question"}]
    req = claude.params(clock.place_clock(history), model="claude-sonnet-5-5", max_tokens=500, tools=None,
                        tool_choice=None)
    assert req["system"][-1]["cache_control"] == {"type": "ephemeral"}
    last = req["messages"][-1]["content"]
    assert last[0] == {"type": "text", "text": "second question", "cache_control": {"type": "ephemeral"}}
    assert last[1]["text"].startswith("[Now: ") and "cache_control" not in last[1]
    # Next turn: everything up to "second question" is byte-identical, so the entry written there is read back.
    nxt = claude.params(clock.place_clock(history + [{"role": "assistant", "content": "second answer"},
                                                    {"role": "user", "content": "third"}]),
                        model="claude-sonnet-5-5", max_tokens=500, tools=None, tool_choice=None)
    assert nxt["messages"][2]["content"][0]["text"] == "second question"
    assert nxt["system"] == req["system"]
    breakpoints = 1 + sum("cache_control" in b for t in nxt["messages"] for b in t["content"]) + 1
    assert breakpoints <= 4


def test_no_history_breakpoint_without_a_stamp_or_text():
    req = claude.params([{"role": "user", "content": "plain"}], model="claude-haiku-5-5", max_tokens=50,
                        tools=None, tool_choice=None)
    assert "cache_control" not in req["messages"][-1]["content"][0] and "system" not in req


# ---- switching models (owner, 2026-11-18): after an API failure, and when a turn gets stuck ----
class _Overloaded(Exception):
    status_code = 529


def test_an_overloaded_model_is_retried_once_on_another(anthropic, monkeypatch):
    used = []

    def call(request, sink, timeout=None):
        used.append(request["model"])
        if len(used) == 1:
            raise _Overloaded("overloaded")
        raw = _block(input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        return "fine", [], claude.usage_of(raw), raw

    monkeypatch.setattr(claude, "call", call)
    assert client.chat([{"role": "user", "content": "hi"}], task="orchestrator.nodes.agent") == "fine"
    assert used == ["claude-sonnet-5-5", "claude-opus-5-5"]


def test_no_switch_after_words_were_shown_or_for_a_bad_request(anthropic, monkeypatch):
    from app.llm import client as client_mod

    class BadRequest(Exception):
        status_code = 400

    tries = []

    def call(request, sink, timeout=None):
        tries.append(request["model"])
        if sink:
            sink("Hello ")
        raise _Overloaded("overloaded")

    monkeypatch.setattr(claude, "call", call)
    token = client_mod.token_sink.set(lambda t: None)
    try:
        with pytest.raises(_Overloaded):
            client.chat([{"role": "user", "content": "hi"}], stream=True, task="orchestrator.nodes.agent")
    finally:
        client_mod.token_sink.reset(token)
    assert len(tries) == 1  # words already reached the person: no second answer

    def bad(request, sink, timeout=None):
        tries.append(request["model"])
        raise BadRequest("bad")

    monkeypatch.setattr(claude, "call", bad)
    with pytest.raises(BadRequest):
        client.chat([{"role": "user", "content": "hi"}], task="orchestrator.nodes.agent")
    assert len(tries) == 2


def test_a_stuck_turn_steps_up_a_tier_within_the_budget_rules(anthropic, monkeypatch):
    from app.llm import model_manager as mm

    msgs = [{"role": "user", "content": "find showtimes"}]
    token = mm.escalated.set(True)
    try:
        assert mm.choose("orchestrator.nodes.agent", msgs).model == "claude-opus-5-5"       # Sonnet -> Opus
        long = msgs + [{"role": "tool", "content": "x"}] * 5                                  # Opus already -> Fable
        assert mm.choose("orchestrator.nodes.agent", long).model == "claude-fable-5-2"
        assert mm.choose("request_manager.guards.triage", msgs).tier == "light"              # sorting isn't stepped up
        real = mm.month_summary
        monkeypatch.setattr(mm, "month_summary", lambda *a: {**real(*a), "mode": "over budget"})
        assert mm.choose("orchestrator.nodes.agent", msgs).tier == "standard"               # budget used up: no Opus
    finally:
        mm.escalated.reset(token)
