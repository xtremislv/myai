"""Thin client over the vLLM OpenAI-compatible endpoint, with metrics + tracing."""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from openai import OpenAI

from app.config import get_settings
from app.observability import (
    llm_latency_seconds,
    llm_requests_total,
    llm_tokens_total,
    tracer,
)

_settings = get_settings()
_client = OpenAI(base_url=_settings.llm_base_url, api_key=_settings.llm_api_key)

# Where the words of a streaming reply go as they arrive. Unset except during a streamed chat.
token_sink: ContextVar[Callable[[str], None] | None] = ContextVar("token_sink", default=None)


def _record_usage(model: str, usage) -> None:
    if usage:
        llm_tokens_total.labels(model=model, kind="prompt").inc(usage.prompt_tokens)
        llm_tokens_total.labels(model=model, kind="completion").inc(usage.completion_tokens)


# Gemma 4 sometimes emits its (empty) thinking channel as text: "<|channel>thought\n<channel|>".
# Sometimes the block is never closed: the whole reply is "<|channel>thought" and the model stops
# (seen 2026-11-04 mid-checkout, where it ended the run with that as the "answer").
_THOUGHT = re.compile(r"<\|channel\>.*?<channel\|>\s*", re.DOTALL)
_UNCLOSED = re.compile(r"<\|channel\>\s*(?:thought\b)?\s*", re.DOTALL)
_MARKER = re.compile(r"<\|channel\>|<channel\|>")
UNCLOSED_KEEP_CHARS = 40  # text after an unclosed marker longer than this is kept as the answer


def strip_thoughts(text: str) -> str:
    """The reply without Gemma's thinking-channel blocks or stray channel markers. An unclosed
    block counts as thinking unless real text follows it; an empty result makes the agent retry."""
    if "<|channel>" not in text and "<channel|>" not in text:
        return text
    text = _THOUGHT.sub("", text)
    match = _UNCLOSED.search(text)
    if match:
        after = text[match.end():]
        text = text[:match.start()] + (after if len(after.strip()) > UNCLOSED_KEEP_CHARS else "")
    return _MARKER.sub("", text).strip()


class _ThoughtFilter:
    """Passes streamed words on, holding back a leading thought-channel block until it ends."""

    def __init__(self, sink: Callable[[str], None]) -> None:
        self._sink = sink
        self._held = ""
        self._open = True  # still at the start, where a thought block can appear

    def feed(self, text: str) -> None:
        if not self._open:
            self._sink(text)
            return
        self._held += text
        if "<|channel>" in self._held:
            if "<channel|>" not in self._held:
                return
            self._held = strip_thoughts(self._held)
        elif len(self._held) < len("<|channel>") and "<|channel>".startswith(self._held.lstrip()):
            return
        self._open = False
        if self._held:
            self._sink(self._held)
        self._held = ""

    def flush(self) -> None:
        if self._held:
            self._sink(strip_thoughts(self._held))
            self._held = ""


def _assembled(streamed: dict[int, dict[str, str]]) -> list[dict[str, Any]]:
    calls = []
    for index in sorted(streamed):
        slot = streamed[index]
        if not slot["name"]:
            continue
        try:
            args: Any = json.loads(slot["args"] or "{}")
        except json.JSONDecodeError:
            args = slot["args"]
        calls.append({"id": slot["id"] or f"call_{index}", "name": slot["name"], "args": args})
    return calls


class Reply(str):
    """The model's text. With tools offered, `tool_calls` holds the calls it asked for, each
    {"id", "name", "args"}; args is a dict, or the raw string if it wasn't valid JSON."""

    tool_calls: list[dict[str, Any]]

    def __new__(cls, text: str, tool_calls: list[dict[str, Any]] | None = None) -> Reply:
        obj = super().__new__(cls, text)
        obj.tool_calls = tool_calls or []
        return obj


def _for_model(message: dict) -> dict:
    """One stored message in the shape the API wants. Native tool calls keep their ids so the
    chat template can pair each result with its call. Older tool results with no id are sent
    as a labelled user turn: Gemma's template silently drops a tool message it can't pair."""
    if message["role"] == "tool":
        if message.get("tool_call_id"):
            return {
                "role": "tool", "tool_call_id": message["tool_call_id"],
                "name": message.get("name", ""), "content": message["content"],
            }
        return {"role": "user", "content": f"Tool result:\n{message['content']}"}
    out = {"role": message["role"], "content": message.get("content") or ""}
    if message["role"] == "assistant" and message.get("tool_calls"):
        out["tool_calls"] = message["tool_calls"]
    return out


def _calls(message) -> list[dict[str, Any]]:
    calls = []
    for tc in getattr(message, "tool_calls", None) or []:
        raw = tc.function.arguments or "{}"
        try:
            args: Any = json.loads(raw)
        except json.JSONDecodeError:
            args = raw  # reported back to the model as invalid arguments
        calls.append({"id": tc.id, "name": tc.function.name, "args": args})
    return calls


def chat(
    messages: list[dict],
    *,
    model: str | None = None,
    stream: bool = False,
    tools: list[dict] | None = None,
    tool_choice: str | None = None,
    **kwargs,
) -> Reply:
    """Single completion. messages = [{"role": "...", "content": "..."}].

    stream=True marks this call as the one producing the user-visible reply: if a streamed
    chat is in progress its words also go to token_sink as they arrive. The full text is
    returned either way, so the graph behaves the same.

    tools offers native function calling (vLLM needs --enable-auto-tool-choice and a
    --tool-call-parser); the calls come back on Reply.tool_calls. With tools and stream=True
    (private chat), words still stream; if the turn ends in tool calls, those words were only
    a preamble, and the chat replaces everything streamed with the final reply when it's done.
    """
    sink = token_sink.get() if stream else None
    model = model or _settings.llm_model
    params = {
        "model": model,
        "messages": [_for_model(m) for m in messages],
        "temperature": kwargs.get("temperature", _settings.llm_temperature),
        "max_tokens": kwargs.get("max_tokens", _settings.llm_max_tokens),
    }
    if tools:
        params["tools"] = tools
        params["tool_choice"] = tool_choice or "auto"
    with tracer.start_as_current_span("llm.chat") as span:
        span.set_attribute("llm.model", model)
        start = time.perf_counter()
        try:
            if sink is None:
                resp = _client.chat.completions.create(**params)
            else:
                parts = []
                usage = None
                streamed_calls: dict[int, dict[str, str]] = {}
                out = _ThoughtFilter(sink)
                chunks = _client.chat.completions.create(
                    **params, stream=True, stream_options={"include_usage": True}
                )
                for chunk in chunks:
                    usage = getattr(chunk, "usage", None) or usage
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta.content:
                        parts.append(delta.content)
                        out.feed(delta.content)
                    for tc in getattr(delta, "tool_calls", None) or []:
                        slot = streamed_calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                        slot["id"] = tc.id or slot["id"]
                        if tc.function is not None:
                            slot["name"] += tc.function.name or ""
                            slot["args"] += tc.function.arguments or ""
                out.flush()
            llm_requests_total.labels(model=model, status="ok").inc()
        except Exception:
            llm_requests_total.labels(model=model, status="error").inc()
            raise
        finally:
            llm_latency_seconds.labels(model=model).observe(time.perf_counter() - start)

        if sink is not None:
            _record_usage(model, usage)
            return Reply(strip_thoughts("".join(parts)), _assembled(streamed_calls))
        _record_usage(model, resp.usage)
        message = resp.choices[0].message
        span.set_attribute("llm.tool_calls", len(getattr(message, "tool_calls", None) or []))
        return Reply(strip_thoughts(message.content or ""), _calls(message))
