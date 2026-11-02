"""Thin client over the vLLM OpenAI-compatible endpoint, with metrics + tracing."""
from __future__ import annotations

import json
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
    --tool-call-parser); the calls come back on Reply.tool_calls. Calls with tools are not
    streamed: the words of a turn that ends in tool calls aren't an answer.
    """
    sink = token_sink.get() if stream and not tools else None
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
                chunks = _client.chat.completions.create(
                    **params, stream=True, stream_options={"include_usage": True}
                )
                for chunk in chunks:
                    usage = getattr(chunk, "usage", None) or usage
                    text = chunk.choices[0].delta.content if chunk.choices else None
                    if text:
                        parts.append(text)
                        sink(text)
            llm_requests_total.labels(model=model, status="ok").inc()
        except Exception:
            llm_requests_total.labels(model=model, status="error").inc()
            raise
        finally:
            llm_latency_seconds.labels(model=model).observe(time.perf_counter() - start)

        if sink is not None:
            _record_usage(model, usage)
            return Reply("".join(parts))
        _record_usage(model, resp.usage)
        message = resp.choices[0].message
        span.set_attribute("llm.tool_calls", len(getattr(message, "tool_calls", None) or []))
        return Reply(message.content or "", _calls(message))
