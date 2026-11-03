"""Web and news search.

With an API key in the vault (``search/tavily`` or ``search/serper``) that provider is used: they
are built for automated clients and give much better results. Without one, web search uses
Bing (its rendered results page plus its RSS feed, filtered for relevance, because Bing
degrades results for automated clients from datacenter addresses), and news uses Bing News
RSS, which is reliable and dated. Results come back as (title, url, snippet, date).
"""
from __future__ import annotations

import base64
import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, quote_plus, urlsplit

from app.browsing.fetch import fetch, post_json
from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Result:
    title: str
    url: str
    snippet: str
    date: str = ""


def _date(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return parsedate_to_datetime(raw).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def _real_url(link: str) -> str:
    """Bing news links are click trackers with the article in ?url=."""
    parts = urlsplit(link)
    if parts.netloc.endswith("bing.com") and parts.path.endswith("apiclick.aspx"):
        target = parse_qs(parts.query).get("url")
        if target:
            return target[0]
    return link


def parse_rss(xml_text: str, limit: int) -> list[Result]:
    root = ET.fromstring(xml_text)
    out = []
    for item in root.iter("item"):
        title = html.unescape(item.findtext("title") or "").strip()
        link = _real_url((item.findtext("link") or "").strip())
        if not title or not link:
            continue
        snippet = html.unescape(item.findtext("description") or "").strip()
        out.append(Result(title, link, snippet[:300], _date(item.findtext("pubDate"))))
        if len(out) >= limit:
            break
    return out


def _market() -> str:
    return get_settings().search_market


def _decode_bing(link: str) -> str:
    """Bing result links go through /ck/a?...&u=a1<base64url of the target>."""
    parts = urlsplit(link)
    if parts.netloc.endswith("bing.com") and parts.path.startswith("/ck/"):
        u = parse_qs(parts.query).get("u", [""])[0]
        if u.startswith("a1"):
            raw = u[2:]
            try:
                return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
            except (ValueError, UnicodeDecodeError):
                return link
    return link


def parse_bing_html(page: str, limit: int) -> list[Result]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(page, "lxml")
    out = []
    for item in soup.select("li.b_algo"):
        a = item.select_one("h2 a[href]")
        if a is None:
            continue
        url = _decode_bing(a["href"])
        if not url.startswith(("http://", "https://")):
            continue
        snippet = item.select_one(".b_caption p, p.b_lineclamp2, p.b_lineclamp3, p.b_lineclamp4, p")
        text = " ".join(snippet.get_text(" ").split()) if snippet else ""
        out.append(Result(" ".join(a.get_text(" ").split()), url, text[:300]))
        if len(out) >= limit:
            break
    return out


_STOP = {
    "the", "and", "for", "with", "what", "when", "where", "which", "who", "how", "why", "are", "was",
    "is", "of", "to", "in", "on", "at", "a", "an", "latest", "today", "current", "now", "new", "best",
    "near", "about", "from", "this", "that", "price", "news",
}


def relevant(query: str, results: list[Result]) -> list[Result]:
    """Drop results that share no meaningful word with the query (Bing's junk for bots)."""
    words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2 and w not in _STOP}
    if not words:
        return results
    return [
        r for r in results
        if words & set(re.findall(r"[a-z0-9]+", f"{r.title} {r.snippet} {r.url}".lower()))
    ]


_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_DATE = rf"(?:{_MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?|\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}(?:,?\s+\d{{4}})?|\d{{4}}-\d{{2}}-\d{{2}})"
_DATES = re.compile(rf"(?:\b(?:from|between|on|for|since)\s+)?\b{_DATE}(?:\s*(?:to|-|–|and|until)\s*{_DATE})?\b", re.IGNORECASE)


def clean_query(query: str) -> str:
    """Drop calendar dates the model adds ("October 27 to November 3 2026"): search engines match
    them as words and return worse results. News search already sorts newest first."""
    cleaned = " ".join(_DATES.sub(" ", query).split())
    return cleaned if len(cleaned) >= 3 else query


def _dedupe(results: list[Result]) -> list[Result]:
    seen, out = set(), []
    for r in results:
        key = r.url.rstrip("/")
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _bing(query: str, limit: int) -> list[Result]:
    from app.browsing.browser import browser

    found: list[Result] = []
    url = f"https://www.bing.com/search?q={quote_plus(query)}&setlang=en&cc={_market().split('-')[-1]}"
    try:
        b = browser()
        found += parse_bing_html(b.call(b.html_of, url), limit)
    except Exception as e:  # noqa: BLE001 - the feed below still works
        logger.warning("browser search failed: %s", type(e).__name__)
    try:
        feed = f"https://www.bing.com/search?format=rss&q={quote_plus(query)}&count={limit}&setlang=en"
        found += parse_rss(fetch(feed).body, limit)
    except Exception as e:  # noqa: BLE001
        logger.warning("feed search failed: %s", type(e).__name__)
    return _dedupe(relevant(query, found))[:limit]


def _tavily(key: str, query: str, limit: int, topic: str) -> list[Result]:
    data = post_json(
        "https://api.tavily.com/search",
        {"query": query, "max_results": limit, "topic": topic, "search_depth": "basic"},
        {"Authorization": f"Bearer {key}"},
    )
    return [
        Result(r.get("title", ""), r.get("url", ""), (r.get("content") or "")[:300], (r.get("published_date") or "")[:10])
        for r in data.get("results", []) if r.get("url")
    ]


def _serper(key: str, query: str, limit: int, topic: str) -> list[Result]:
    country = _market().split("-")[-1].lower()
    endpoint = "news" if topic == "news" else "search"
    data = post_json(
        f"https://google.serper.dev/{endpoint}", {"q": query, "gl": country, "num": limit}, {"X-API-KEY": key}
    )
    rows = data.get("news" if topic == "news" else "organic", [])
    return [Result(r.get("title", ""), r.get("link", ""), (r.get("snippet") or "")[:300], r.get("date", "")) for r in rows if r.get("link")]


PROVIDERS = {"tavily": _tavily, "serper": _serper}


def web(query: str, limit: int = 8, api: tuple[str, str] | None = None) -> list[Result]:
    """api: (provider, key) from the vault, or None for keyless Bing."""
    query = clean_query(query)
    if api is not None:
        try:
            return PROVIDERS[api[0]](api[1], query, limit, "general")[:limit]
        except Exception as e:  # noqa: BLE001 - fall back rather than fail the task
            logger.warning("%s search failed: %s", api[0], type(e).__name__)
    return _bing(query, limit)


def news(query: str, limit: int = 8, api: tuple[str, str] | None = None) -> list[Result]:
    query = clean_query(query)
    if api is not None:
        try:
            return PROVIDERS[api[0]](api[1], query, limit, "news")[:limit]
        except Exception as e:  # noqa: BLE001
            logger.warning("%s news failed: %s", api[0], type(e).__name__)
    url = f"https://www.bing.com/news/search?format=rss&q={quote_plus(query)}&mkt={_market()}"
    results = parse_rss(fetch(url).body, limit)
    return sorted(results, key=lambda r: r.date, reverse=True)


def render(results: list[Result]) -> str:
    if not results:
        return "No results."
    return "\n".join(
        f"{i}. {r.title}" + (f" ({r.date})" if r.date else "") + f"\n   {r.url}" + (f"\n   {r.snippet}" if r.snippet else "")
        for i, r in enumerate(results, 1)
    )
