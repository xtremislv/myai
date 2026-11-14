"""Which tools to show the model for a message.

Every tool definition is sent with every call, and on this GPU a longer prompt makes the model
write more slowly (all 58 private tools: ~8k tokens, 41 tokens/s; one group: ~2.7k, ~63 tokens/s).
So each message is sorted into tool groups by a short model call (run alongside triage), and the
agent is shown only those groups' tools, plus:
  - tools in no group (a department's own tools, more_tools): always;
  - groups whose tools were used in the last few turns, so follow-ups ("send it") keep them;
  - groups the model asked for this turn with more_tools, its way out of a wrong guess.

This only changes what the model is shown. Dispatch (nodes.run_tool) still accepts any tool of the
department, and every call is still ACL-checked and confirmed as before. If sorting fails, the
model gets every tool, as before. Measure changes with scripts/eval_toolgroups.py.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections import OrderedDict
from typing import Any

from app.departments.packs import (
    BROWSE_TOOLS,
    DATA_TOOLS,
    FILE_TOOLS,
    LOOKUP_TOOLS,
    MAIL_TOOLS,
    REMINDER_TOOLS,
    TEMPLATE_TOOLS,
    WALLET_TOOLS,
)
from app.orchestrator.context import tool_groups

logger = logging.getLogger(__name__)

GROUPS: dict[str, tuple[str, ...]] = {
    "web": (*LOOKUP_TOOLS, *BROWSE_TOOLS, "browser_commit", *WALLET_TOOLS),
    "mail": MAIL_TOOLS,
    "files": FILE_TOOLS,
    "reminders": REMINDER_TOOLS,
    "data": DATA_TOOLS,
    "templates": TEMPLATE_TOOLS,
}
GROUP_OF: dict[str, str] = {tool: group for group, tools in GROUPS.items() for tool in tools}

DESCRIPTIONS = {
    "web": "anything on the internet: search, news, products, gadgets, brand stores and prices, new launches, weather, forums, GitHub, YouTube videos "
           "and channels (incl. our TechWiser/GadgetWiser channel stats), open or read "
           "a web page or link, deep research; and using websites in a browser: sign in, fill forms, book, "
           "order, buy, pay, track an order, saved cards/addresses/logins",
    "mail": "email: read, search, summarize, open, send, reply or forward mail, mail to a spreadsheet",
    "files": "files: list, read, write, save, edit, move, delete, restore; and sharing a note or file with a "
             "colleague on this platform",
    "reminders": "set, list or cancel reminders",
    "data": "anything long or numeric: analyse data with Python, charts, dashboards, tables, summarize a very "
            "long document or attachment, background jobs and their status",
    "templates": "documents made from templates (letters, invoices, forms): list, read, fill, save templates",
}

PROMPT = """Decide which tool groups the assistant may need to answer the latest message. Groups:
{groups}

Rules: pick every group that could be needed, including for follow-ups ("yes", "send it", "do that")
that continue earlier work. Pick none for greetings, thanks, opinions, writing or explaining from
general knowledge, or questions about the conversation itself. When unsure, include the group.
Reply with JSON only: {{"groups": ["..."]}}"""

# Departments with this few tools get them all: nothing worth saving.
MIN_TOOLS_TO_SELECT = 12
# Groups used in this many recent user turns stay available (follow-ups).
RECENT_TURNS = 3
RECENT_CHARS = 300
TIMEOUT = 20  # seconds; on timeout the model simply gets every tool
_CACHE_SIZE = 256
_cache: OrderedDict[str, frozenset[str] | None] = OrderedDict()
_lock = threading.Lock()


def classify(message: str, recent: list[dict[str, Any]], available: set[str] | None = None) -> frozenset[str] | None:
    """The groups a message may need, or None if the model gave nothing usable."""
    from app.llm import client as llm

    names = [g for g in GROUPS if available is None or g in available]
    if not names:
        return frozenset()
    system = PROMPT.format(groups="\n".join(f"- {g}: {DESCRIPTIONS[g]}" for g in names))
    earlier = [m for m in recent if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
               and m.get("content")]
    if earlier:
        system += "\n\nEarlier in this conversation:\n" + "\n".join(
            f"{m['role']}: {m['content'][:RECENT_CHARS]}" for m in earlier[-4:])
    try:
        reply = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": message}],
                         temperature=0, max_tokens=60, timeout=TIMEOUT)
    except Exception:  # noqa: BLE001 - every tool, as before
        logger.warning("tool sorting unavailable; offering every tool")
        return None
    match = re.search(r"\{.*\}", str(reply), re.DOTALL)
    try:
        picked = json.loads(match.group())["groups"] if match else None
    except (json.JSONDecodeError, KeyError, TypeError):
        picked = None
    if not isinstance(picked, list):
        return None
    return frozenset(g for g in picked if g in names)


def _turn(messages: list[dict[str, Any]]) -> tuple[int, str]:
    """Index and text of the latest user message (this turn's request)."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return i, m["content"]
    return -1, ""


def _classify_cached(department: str | None, messages: list[dict[str, Any]], available: set[str]) -> frozenset[str] | None:
    """Sort once per turn: the agent asks again after every round of tool calls. The key is a hash
    (no conversation text is kept) of the department, this turn's message and what came before it."""
    index, message = _turn(messages)
    before = messages[max(0, index - 4):max(index, 0)]
    key = hashlib.sha256(json.dumps([department, message, [m.get("content") for m in before]],
                                    default=str).encode()).hexdigest()
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    found = classify(message, before, available)
    with _lock:
        _cache[key] = found
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return found


def _used_groups(messages: list[dict[str, Any]]) -> set[str]:
    """Groups of tools called in the last RECENT_TURNS user turns, and groups asked for with more_tools."""
    users = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    start = users[-RECENT_TURNS] if len(users) >= RECENT_TURNS else 0
    found: set[str] = set()
    for m in messages[start:]:
        for call in m.get("tool_calls") or []:
            name = (call.get("function") or {}).get("name") or call.get("name")
            if name in GROUP_OF:
                found.add(GROUP_OF[name])
            elif name == "more_tools":
                found |= requested_groups(call)
    return found


def requested_groups(call: dict[str, Any]) -> set[str]:
    raw = (call.get("function") or {}).get("arguments", call.get("args"))
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {"groups": raw}
    value = (raw or {}).get("groups", "") if isinstance(raw, dict) else ""
    words = value if isinstance(value, list) else re.split(r"[\s,]+", str(value))
    found = set()
    for word in (str(w).strip().lower() for w in words):
        if word in GROUPS:
            found.add(word)
        elif word in GROUP_OF:  # a tool's name ("weather"): its group
            found.add(GROUP_OF[word])
    return found


def select(schemas: list[dict[str, Any]], department: str | None, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tool definitions to send for this step: a subset of `schemas`, in their order."""
    if len(schemas) <= MIN_TOOLS_TO_SELECT:
        return schemas
    available = {GROUP_OF[t["function"]["name"]] for t in schemas if t["function"]["name"] in GROUP_OF}
    _, message = _turn(messages)
    sorted_for = tool_groups.get()  # (hash of the message it was sorted for, groups), set by the chat route
    if sorted_for is not None and sorted_for[0] == message_hash(message):
        groups = sorted_for[1]
    else:
        groups = _classify_cached(department, messages, available)
    if groups is None:
        return schemas
    wanted = set(groups) | _used_groups(messages)
    return [t for t in schemas if GROUP_OF.get(t["function"]["name"]) in wanted or t["function"]["name"] not in GROUP_OF]


def message_hash(message: str) -> str:
    return hashlib.sha256(message.encode()).hexdigest()


def sort_message(message: str, recent: list[dict[str, Any]]) -> tuple[str, frozenset[str] | None]:
    """For the chat route, alongside triage: the groups for a new message (None: every tool),
    tagged with the message's hash so they are only ever used for that message. Nothing about the
    message is logged: this runs for private chats too."""
    return message_hash(message), classify(message, recent)
