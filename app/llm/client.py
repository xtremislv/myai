"""Thin client over the vLLM OpenAI-compatible endpoint, with metrics + tracing."""
from __future__ import annotations

import time
from collections.abc import Callable
from contextvars import ContextVar

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


def chat(messages: list[dict], *, model: str | None = None, stream: bool = False, **kwargs) -> str:
    """Single completion. messages = [{"role": "...", "content": "..."}].

    stream=True marks this call as the one producing the user-visible reply: if a streamed
    chat is in progress its words also go to token_sink as they arrive. The full text is
    returned either way, so the graph behaves the same.
    """
    sink = token_sink.get() if stream else None
    model = model or _settings.llm_model
    params = {
        "model": model,
        "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        "temperature": kwargs.get("temperature", _settings.llm_temperature),
        "max_tokens": kwargs.get("max_tokens", _settings.llm_max_tokens),
    }
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
            return "".join(parts)
        _record_usage(model, resp.usage)
        return resp.choices[0].message.content or ""
