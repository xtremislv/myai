"""Thin client over the vLLM OpenAI-compatible endpoint, with metrics + tracing."""
from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from openai import OpenAI

from app.config import get_settings
from app.llm.governor import Paused
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


YIELD_POLL_SECONDS = 0.25  # how often a background call checks whether to give way


class _GaveWay(threading.Event):
    done: threading.Event


def _watch(chunks, stop_if: Callable[[], bool]) -> _GaveWay:
    """Close a background call's stream as soon as stop_if() says live work needs the room, even
    while the model is still reading the prompt (when no pieces arrive to check between). The
    model server cancels a request whose connection closes."""
    gave_way = _GaveWay()
    gave_way.done = threading.Event()

    def watch() -> None:
        while not gave_way.done.wait(YIELD_POLL_SECONDS):
            if stop_if():
                gave_way.set()
                try:
                    chunks.response.close()
                except Exception:  # noqa: BLE001 - already closed
                    pass
                return

    threading.Thread(target=watch, daemon=True, name="llm-yield").start()
    return gave_way


def _discard(_text: str) -> None:
    pass


class Reply(str):
    """The model's text. With tools offered, `tool_calls` holds the calls it asked for, each
    {"id", "name", "args"}; args is a dict, or the raw string if it wasn't valid JSON."""

    tool_calls: list[dict[str, Any]]

    def __new__(cls, text: str, tool_calls: list[dict[str, Any]] | None = None) -> Reply:
        obj = super().__new__(cls, text)
        obj.tool_calls = tool_calls or []
        return obj


# Prompts are measured roughly (no tokenizer here): Gemma averages ~4 characters per token on
# English, less on JSON and other scripts, so 3 keeps a margin.
CHARS_PER_TOKEN = 3
CONTEXT_MARGIN_TOKENS = 512
MIN_REPLY_TOKENS = 256


def _estimate_tokens(messages: list[dict], tools: list | None = None) -> int:
    size = sum(len(json.dumps(m, ensure_ascii=False, default=str)) for m in messages)
    if tools:
        size += len(json.dumps(tools, ensure_ascii=False, default=str))
    return size // CHARS_PER_TOKEN


def fit_to_context(messages: list[dict], tools: list | None, max_tokens: int,
                   context: int | None = None) -> tuple[list[dict], int]:
    """The messages and reply length that fit the model's context window. Oldest turns go
    first; system messages and the latest turn always stay, and what's left starts on a user
    turn so no tool result is cut off from its call. The reply length shrinks only if dropping
    turns isn't enough."""
    context = context or _settings.llm_context_tokens
    budget = context - max_tokens - CONTEXT_MARGIN_TOKENS
    if _estimate_tokens(messages, tools) <= budget:
        return messages, max_tokens
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    users = [i for i, m in enumerate(rest) if m.get("role") == "user"]
    for start in users[1:]:  # drop whole turns, oldest first, never the last one
        rest_now = rest[start:]
        if _estimate_tokens(system + rest_now, tools) <= budget:
            return [*system, *rest_now], max_tokens
    kept = [*system, *(rest[users[-1]:] if users else rest)]
    room = context - CONTEXT_MARGIN_TOKENS - _estimate_tokens(kept, tools)
    return kept, max(MIN_REPLY_TOKENS, min(max_tokens, room))


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
    """One model call. Background work waits for a slot that shrinks while people are chatting
    (app/llm/governor.py); everything else counts as live load."""
    from app.llm.governor import background, governor

    if background.get():
        return governor.run(lambda should_yield: _chat(
            messages, model=model, stream=stream, tools=tools, tool_choice=tool_choice, stop_if=should_yield, **kwargs))
    with governor.live():
        return _chat(messages, model=model, stream=stream, tools=tools, tool_choice=tool_choice, **kwargs)


def _chat(
    messages: list[dict],
    *,
    model: str | None = None,
    stream: bool = False,
    tools: list[dict] | None = None,
    tool_choice: str | None = None,
    stop_if: Callable[[], bool] | None = None,
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
    if stop_if is not None and sink is None:
        sink = _discard  # stream, so the call can be stopped part-way
    model = model or _settings.llm_model
    params = {
        "model": model,
        "temperature": kwargs.get("temperature", _settings.llm_temperature),
    }
    params["messages"], params["max_tokens"] = fit_to_context(
        [_for_model(m) for m in messages], tools, kwargs.get("max_tokens", _settings.llm_max_tokens))
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
                gave_way = _watch(chunks, stop_if) if stop_if is not None else None
                try:
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
                except Exception:
                    if gave_way is not None and gave_way.is_set():
                        raise Paused from None
                    raise
                finally:
                    if gave_way is not None:
                        gave_way.done.set()
                if gave_way is not None and gave_way.is_set():
                    raise Paused  # closed just as it ended: the reply may be cut short
                out.flush()
            llm_requests_total.labels(model=model, status="ok").inc()
        except Paused:
            llm_requests_total.labels(model=model, status="paused").inc()
            raise
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
