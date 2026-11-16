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
        monkeypatch.setattr(s, "llm_fast_model", "claude-haiku-5-5")
        monkeypatch.setattr(s, "llm_thinking", "off")
        monkeypatch.setattr(s, "llm_effort", "")
        monkeypatch.setattr(s, "llm_context_tokens", 200_000)
    fake = FakeAPI()
    monkeypatch.setattr(claude, "_client", fake)
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


def test_short_tasks_use_the_fast_model_without_thinking(anthropic):
    from app.request_manager import guards  # noqa: F401 - the task name is what matters

    assert claude.model_for("request_manager.guards.triage") == "claude-haiku-5-5"
    assert claude.model_for("scout.judge") == "claude-sonnet-5-5"
    assert claude.thinking_params("claude-haiku-5-5") == {"thinking": {"type": "disabled"}}
    assert claude.thinking_params("claude-opus-5-5") == {}  # always thinks: nothing to send
    req = claude.params([{"role": "user", "content": "x"}], model="claude-opus-5-5", max_tokens=120,
                        tools=None, tool_choice=None)
    assert req["max_tokens"] == claude.THINKING_MIN_TOKENS  # room to think before a short verdict


def test_tool_choice_none_and_adaptive_thinking(anthropic, monkeypatch):
    monkeypatch.setattr(claude.get_settings(), "llm_thinking", "adaptive")
    monkeypatch.setattr(claude.get_settings(), "llm_effort", "low")
    req = claude.params([{"role": "user", "content": "x"}], model="claude-sonnet-5-5", max_tokens=500,
                        tools=[{"type": "function", "function": {"name": "a", "parameters": {}}}], tool_choice="none")
    assert req["tool_choice"] == {"type": "none"} and req["thinking"] == {"type": "adaptive"}
    assert req["output_config"] == {"effort": "low"} and req["max_tokens"] == claude.THINKING_MIN_TOKENS
