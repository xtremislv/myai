"""Claude (Anthropic API) behind app/llm/client.py's `chat()`, for LLM_PROVIDER=anthropic.

The rest of the app speaks the OpenAI chat shape that vLLM used: messages with roles system/user/assistant/
tool, assistant `tool_calls`, and tools as {"type": "function", "function": {...}}. This module converts that
shape to the Messages API and back, so no caller changes:

  - system messages before the conversation become `system`; later ones become a user note;
  - assistant tool calls become `tool_use` blocks, tool messages `tool_result` blocks in the next user turn.
    The API insists every tool_use is answered right after it, so a call with no stored result (a declined
    approval, a cut-off run) gets a short "did not run" result, and a result whose call is gone is sent as
    plain text, like client._for_model does for Gemma;
  - Claude's thinking blocks are never sent back. The app rewrites its history (fit_to_context, compacted
    tool results, the clock stamp), and replaying a thinking block after an edit is refused; leaving them
    out is always allowed.

Which model: app/llm/model_manager.py (Haiku / Sonnet / Opus / Fable by the work, the monthly mix and budget).
Thinking is off by default where the model allows a choice (LLM_THINKING=adaptive turns it on); Claude Opus 5.5
and Claude Fable 5.2 always think.
Temperature is not sent: Claude Haiku 5.5 refuses non-default sampling settings.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

NO_RESULT = "(This call did not run: there is no result for it.)"
EMPTY = "(empty)"
# With thinking possible, a tiny max_tokens (a 120-token verdict) would be spent before any answer.
THINKING_MIN_TOKENS = 4096

# The server is ~180 ms from the API; a fresh connection (DNS + TCP + TLS) costs ~0.6 s. The SDK's default pool
# drops idle connections after 5 s, so every chat message paid that, twice (triage and tool picking run side by
# side). HTTP/2 carries parallel calls on one connection, kept for 5 minutes and warmed by /readyz every minute
# (measured 2026-11-17: two parallel calls after idling 1.7 s -> 0.57 s).
KEEPALIVE_SECONDS = 300

_client = None
_client_lock = threading.Lock()


def _http_client():
    import anthropic
    import httpx2

    try:
        import h2  # noqa: F401 - HTTP/2 needs it
        http2 = True
    except ImportError:
        http2 = False
    limits = httpx2.Limits(max_connections=1000, max_keepalive_connections=100, keepalive_expiry=KEEPALIVE_SECONDS)
    return anthropic.DefaultHttpxClient(limits=limits, http2=http2)


def client():
    """The SDK client, made on first use so tests and the vLLM setup never need a key."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import anthropic

                s = get_settings()
                _client = anthropic.Anthropic(api_key=s.anthropic_api_key or None, max_retries=3,
                                              http_client=_http_client())
    return _client


# ---- which model, which settings ----
def always_thinks(model: str) -> bool:
    return model.startswith(("claude-opus-5-5", "claude-fable-5"))


def thinking_params(model: str) -> dict[str, Any]:
    """`thinking` / `output_config` for this model under LLM_THINKING and LLM_EFFORT."""
    s = get_settings()
    out: dict[str, Any] = {}
    effort = (s.llm_effort or "").strip()
    if always_thinks(model):
        pass  # Claude Opus 5.5 and Fable: always adaptive; "disabled" is a 400
    elif s.llm_thinking == "adaptive":
        out["thinking"] = {"type": "adaptive"}
    elif model.startswith("claude-sonnet-5-5"):
        out["thinking"] = {"type": "between_tools"}  # Sonnet 5.5's "off" (effort high or below)
        if effort in ("xhigh", "max"):
            effort = "high"
    elif model.startswith(("claude-haiku-5-5", "claude-opus-5", "claude-opus-4", "claude-sonnet-4", "claude-haiku-4")):
        out["thinking"] = {"type": "disabled"}
        if effort in ("xhigh", "max"):
            effort = "high"
    if effort:
        out["output_config"] = {"effort": effort}
    return out


def thinks(model: str) -> bool:
    return always_thinks(model) or get_settings().llm_thinking == "adaptive"


# ---- OpenAI shape -> Messages API ----
_ID = re.compile(r"[^a-zA-Z0-9_-]")


def _id(raw: str, index: int) -> str:
    return _ID.sub("_", raw or "")[:64] or f"call_{index}"


def _text(content: Any) -> str:
    if isinstance(content, list):  # OpenAI content parts: only text is used by this app
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return "" if content is None else str(content)


def convert_tools(tools: list[dict] | None) -> list[dict]:
    out = []
    for t in tools or []:
        fn = t.get("function", t)
        out.append({"name": fn["name"], "description": fn.get("description") or fn["name"],
                    "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
    return out


def convert_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """(system text, messages) for the Messages API, from OpenAI-shaped messages."""
    system: list[str] = []
    turns: list[dict] = []  # {"role", "content": [blocks]}, consecutive roles merged
    started = False
    open_calls: list[str] = []  # tool_use ids of the latest assistant turn still waiting for results

    def add(role: str, blocks: list[dict]) -> None:
        if not blocks:
            return
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(blocks)
        else:
            turns.append({"role": role, "content": list(blocks)})

    def close_calls() -> None:
        """Answer the latest assistant's unanswered calls before anything else is said."""
        if open_calls:
            add("user", [{"type": "tool_result", "tool_use_id": i, "content": NO_RESULT} for i in open_calls])
            open_calls.clear()

    for n, m in enumerate(messages):
        role = m.get("role")
        text = _text(m.get("content"))
        if role == "system" and not started:
            if text.strip():
                system.append(text)
            continue
        started = True
        if role == "tool":
            call_id = _id(m.get("tool_call_id") or "", n)
            if m.get("tool_call_id") and call_id in open_calls:
                open_calls.remove(call_id)
                add("user", [{"type": "tool_result", "tool_use_id": call_id, "content": text or EMPTY}])
            else:  # its call is gone (trimmed, or an old text-format call): plain text
                close_calls()
                add("user", [{"type": "text", "text": f"Tool result:\n{text or EMPTY}"}])
            continue
        close_calls()
        if role == "assistant":
            blocks: list[dict] = []
            if text.strip():
                blocks.append({"type": "text", "text": text})
            for i, tc in enumerate(m.get("tool_calls") or []):
                fn = tc.get("function", {})
                raw = fn.get("arguments")
                args: Any = raw
                if isinstance(raw, str):
                    try:
                        args = json.loads(raw or "{}")
                    except ValueError:
                        args = {"raw_arguments": raw}
                if not isinstance(args, dict):
                    args = {"value": args}
                call_id = _id(tc.get("id") or "", n * 100 + i)
                blocks.append({"type": "tool_use", "id": call_id, "name": fn.get("name", "unknown"), "input": args})
                open_calls.append(call_id)
            add("assistant", blocks)
        else:  # user, or a system message mid-conversation
            note = f"[System note]\n{text}" if role == "system" else text
            add("user", [{"type": "text", "text": note if note.strip() else EMPTY}])
    close_calls()
    # The API wants a user turn first and last (Claude's newer models refuse a prefilled assistant reply).
    if not turns or turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": [{"type": "text", "text": "(Earlier conversation omitted.)"}]})
    if turns[-1]["role"] != "user":
        turns.append({"role": "user", "content": [{"type": "text", "text": "Continue."}]})
    # tool_result blocks must open their user turn, before any text.
    for t in turns:
        if t["role"] == "user":
            t["content"].sort(key=lambda b: b["type"] != "tool_result")
    return "\n\n".join(system), turns


# The clock stamp app/orchestrator/clock.place_clock puts on the latest user message.
_STAMP = re.compile(r"\n+(\[Now: [^\]\n]*\])\s*$")
CACHE = {"type": "ephemeral"}


def _mark_history(turns: list[dict]) -> None:
    """A cache breakpoint on the latest user message, before its clock stamp.

    The stamp moves to the next user message on the next turn, so the history up to here is only
    read back from the cache if an entry ends exactly here (the automatic breakpoint at the very end
    includes the stamp, and is only reused by this turn's own tool rounds)."""
    last = turns[-1]
    if last["role"] != "user":
        return
    blocks = last["content"]
    for i in range(len(blocks) - 1, -1, -1):
        block = blocks[i]
        if block.get("type") != "text":
            continue
        match = _STAMP.search(block["text"])
        if not match or not block["text"][:match.start()].strip():
            return
        blocks[i:i + 1] = [{"type": "text", "text": block["text"][:match.start()], "cache_control": CACHE},
                           {"type": "text", "text": match.group(1)}]
        return


def params(messages: list[dict], *, model: str, max_tokens: int, tools: list[dict] | None,
           tool_choice: str | None) -> dict[str, Any]:
    system, turns = convert_messages(messages)
    _mark_history(turns)
    out: dict[str, Any] = {
        "model": model,
        "max_tokens": max(max_tokens, THINKING_MIN_TOKENS) if thinks(model) else max_tokens,
        "messages": turns,
        # Up to three 5-minute cache entries (prompt caching measured at 0 reads on 2026-11-17 with only the
        # automatic one, which sits after each request's unique tail):
        #   tools + system prompt: shared by every call with the same prompt and tool groups, any person;
        #   the history up to the latest user message, before its clock stamp (_mark_history): the next turn;
        #   automatic, at the end: this turn's next tool round.
        "cache_control": CACHE,
        **thinking_params(model),
    }
    if system:
        out["system"] = [{"type": "text", "text": system, "cache_control": CACHE}]
    if tools:
        out["tools"] = convert_tools(tools)
        # "any"/"tool" (forced calls) are refused by the newest models; the app only uses auto and none.
        out["tool_choice"] = {"type": "none" if tool_choice == "none" else "auto"}
    return out


# ---- Messages API -> what client.chat returns ----
def usage_of(usage: Any) -> SimpleNamespace | None:
    """The response's usage in the OpenAI shape client._record_usage reads (prompt = all input, cached
    included)."""
    if usage is None:
        return None
    fresh = getattr(usage, "input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return SimpleNamespace(prompt_tokens=fresh + read + write, completion_tokens=getattr(usage, "output_tokens", 0) or 0,
                           prompt_tokens_details=SimpleNamespace(cached_tokens=read))


def reply_parts(message: Any) -> tuple[str, list[dict[str, Any]]]:
    text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
    calls = [{"id": b.id, "name": b.name, "args": b.input if isinstance(b.input, dict) else {"value": b.input}}
             for b in message.content if getattr(b, "type", "") == "tool_use"]
    if message.stop_reason == "refusal":
        logger.warning("model declined a request (stop_reason refusal)")
    elif message.stop_reason == "max_tokens":
        logger.info("reply cut at max_tokens")
    return text, calls


def call(request: dict[str, Any], sink: Callable[[str], None] | None, timeout: float | None = None):
    """One request: streamed to `sink` as the words arrive when given. Returns (text, calls, usage, raw usage)."""
    api = client().with_options(timeout=timeout) if timeout else client()
    if sink is None:
        message = api.messages.create(**request)
    else:
        with api.messages.stream(**request) as stream:
            for event in stream:
                if event.type == "text":
                    sink(event.text)
            message = stream.get_final_message()
    text, calls = reply_parts(message)
    return text, calls, usage_of(message.usage), message.usage


# Background work (big summaries, mail sheets, scouts) shares the account's rate limit with chats,
# not a GPU: a few at a time is enough to keep live chats quick.
_background = threading.BoundedSemaphore(max(1, get_settings().llm_background_concurrency))


def background_slot() -> threading.BoundedSemaphore:
    return _background
