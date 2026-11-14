"""Keep the links in a final answer to the ones tools actually returned.

Gemma shortens long product links when it copies them: Shopify handles run to 150+ characters, and on
2026-11-14 "nano-power-bank-10000-mah-22-5w-power-delivery-..." came out as "nano-power-bank-1", a page
that doesn't exist. A link to a site the tools returned links for this turn, but which isn't one of
them, is replaced by the returned link it resembles most (enough words of its path in common). Links
to sites no tool returned, and links with nothing close enough, are left alone: there is nothing to
check them against.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

URL = re.compile(r"https?://[^\s)\]>\"'|<]+")
_TRAIL = ".,;:!?*_`"
_SKIP = {"https", "http", "www", "com", "in", "html", "products", "product", "dp", "p", "the", "and", "for", "with"}


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _words(url: str) -> set[str]:
    parts = urlsplit(url)
    return {w for w in re.split(r"[^a-z0-9]+", f"{parts.path} {parts.query}".lower()) if w and w not in _SKIP}


def sources(messages: list[dict[str, Any]], notes: list[str] | None = None) -> list[str]:
    """This turn's tool results (everything after the latest user message) and specialists' reports."""
    start = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1) + 1
    texts = [str(m.get("content") or "") for m in messages[start:] if m.get("role") == "tool"]
    return texts + [str(n) for n in notes or []]


def fix_links(text: str, texts: list[str]) -> str:
    seen = {u.rstrip(_TRAIL) for t in texts for u in URL.findall(t)}
    if not text or not seen:
        return text
    by_host: dict[str, list[str]] = {}
    for u in seen:
        by_host.setdefault(_host(u), []).append(u)

    def repl(m: re.Match) -> str:
        url = m.group(0)
        core = url.rstrip(_TRAIL)
        tail = url[len(core):]
        if core in seen:
            return url
        candidates = by_host.get(_host(core))
        mine = _words(core)
        if not candidates or not mine:
            return url
        best = max(candidates, key=lambda u: (len(mine & _words(u)), -len(_words(u) - mine)))
        shared = len(mine & _words(best))
        if shared >= 2 and shared >= 0.6 * len(mine):
            return best + tail
        return url

    return URL.sub(repl, text)
