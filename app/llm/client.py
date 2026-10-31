"""Thin client over the vLLM OpenAI-compatible endpoint, with metrics + tracing."""
from __future__ import annotations

import time

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


def chat(messages: list[dict], *, model: str | None = None, **kwargs) -> str:
    """Single completion. messages = [{"role": "...", "content": "..."}]."""
    model = model or _settings.llm_model
    params = {
        "model": model,
        "messages": messages,
        "temperature": kwargs.get("temperature", _settings.llm_temperature),
        "max_tokens": kwargs.get("max_tokens", _settings.llm_max_tokens),
    }
    with tracer.start_as_current_span("llm.chat") as span:
        span.set_attribute("llm.model", model)
        start = time.perf_counter()
        try:
            resp = _client.chat.completions.create(**params)
            llm_requests_total.labels(model=model, status="ok").inc()
        except Exception:
            llm_requests_total.labels(model=model, status="error").inc()
            raise
        finally:
            llm_latency_seconds.labels(model=model).observe(time.perf_counter() - start)

        if resp.usage:
            llm_tokens_total.labels(model=model, kind="prompt").inc(resp.usage.prompt_tokens)
            llm_tokens_total.labels(model=model, kind="completion").inc(resp.usage.completion_tokens)
        return resp.choices[0].message.content or ""
