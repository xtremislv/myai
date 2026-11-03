"""Web and news search.

With an API key in the vault (``search/tavily`` or ``search/serper``) that provider is used: they
are built for automated clients and give much better results. Without one, web search uses
Bing's RSS feed, then Bing News, then Bing's rendered page, keeping only results that match
most of the query (measured 2026-11-03: Bing gives this server results that match one word, or
none, for most queries), and news uses Bing News RSS, which is reliable and dated.

scholarly() searches research papers: arXiv, Crossref and PubMed, which are reachable here and
need no key. Results come back as (title, url, snippet, date).
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


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in _STOP}


def _matches(word: str, haystack: set[str]) -> bool:
    """A query word is found if a result word starts with it, or the reverse ("battery" and
    "batteries" share a stem of at least 5 letters)."""
    return any(h == word or (len(word) >= 5 and len(h) >= 5 and (h.startswith(word[:5]) and word.startswith(h[:5])))
               for h in haystack)


def relevance(query: str, result: Result) -> float:
    words = _words(query)
    if not words:
        return 1.0
    haystack = set(re.findall(r"[a-z0-9]+", f"{result.title} {result.snippet} {result.url}".lower()))
    return sum(_matches(w, haystack) for w in words) / len(words)


def relevant(query: str, results: list[Result], threshold: float = 0.5) -> list[Result]:
    """Keep results that match at least half the meaningful query words (two or more when the
    query has three or more), best first. Bing gives automated clients from datacenter
    addresses results that often match one word only ("solid state battery" -> "SOLID
    principles"); those are dropped rather than handed to the agent as if they were relevant."""
    import math

    words = _words(query)
    if not words:
        return results
    need = max(threshold, math.ceil(len(words) * 0.6) / len(words) if len(words) >= 3 else 0)
    scored = [(relevance(query, r), i, r) for i, r in enumerate(results)]
    return [r for score, _, r in sorted(scored, key=lambda t: (-t[0], t[1])) if score >= need]


def reachable(results: list[Result]) -> list[Result]:
    """Drop results on sites this server can't open (DNS-poisoned or private names), so agents
    don't spend steps on links that will fail. Lookups are cached by the network guard."""
    from concurrent.futures import ThreadPoolExecutor

    from app.browsing.netguard import Blocked, resolve_public

    def ok(r: Result) -> bool:
        parts = urlsplit(r.url)
        try:
            resolve_public(parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80))
            return True
        except Blocked:
            return False

    if not results:
        return results
    with ThreadPoolExecutor(min(8, len(results))) as pool:
        keep = list(pool.map(ok, results))
    return [r for r, k in zip(results, keep) if k]


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


ENOUGH = 3  # relevant results before trying the next (slower) source


def parse_ddg(page: str, limit: int) -> list[Result]:
    """DuckDuckGo's plain HTML results page (no JavaScript, no key). Ads are skipped."""
    from bs4 import BeautifulSoup

    out = []
    for item in BeautifulSoup(page, "lxml").select("div.result"):
        if "result--ad" in (item.get("class") or []):
            continue
        a = item.select_one("a.result__a[href]")
        if a is None:
            continue
        href = a["href"]
        target = parse_qs(urlsplit(href).query).get("uddg", [""])[0] if "uddg=" in href else href
        if not target.startswith(("http://", "https://")):
            continue
        snippet = item.select_one(".result__snippet")
        out.append(Result(" ".join(a.get_text(" ").split()), target,
                          " ".join(snippet.get_text(" ").split())[:300] if snippet else ""))
        if len(out) >= limit:
            break
    return out


def _ddg(query: str, limit: int) -> list[Result]:
    """Reachable only through OUTBOUND_PROXY on this server. HTTP 202 means it is rate-limiting."""
    page = fetch(f"https://html.duckduckgo.com/html/?q={quote_plus(query)}&kl={_market().split('-')[-1].lower()}-en")
    return parse_ddg(page.body, limit) if page.status == 200 else []


def _bing(query: str, limit: int) -> list[Result]:
    """Keyless search, best source first: DuckDuckGo's HTML page (good results, reachable through
    the proxy), then Bing's RSS feed, Bing News, and Bing's rendered results page."""
    from app.browsing.browser import browser

    found: list[Result] = []

    def good() -> list[Result]:
        return reachable(_dedupe(relevant(query, found)))

    try:
        found += _ddg(query, limit)
    except Exception as e:  # noqa: BLE001 - unreachable without the proxy; Bing below
        logger.info("duckduckgo unavailable: %s", type(e).__name__)
    if len(good()) >= ENOUGH:
        return good()[:limit]
    try:
        feed = f"https://www.bing.com/search?format=rss&q={quote_plus(query)}&count={max(limit, 10)}&setlang=en"
        found += parse_rss(fetch(feed).body, max(limit, 10))
    except Exception as e:  # noqa: BLE001
        logger.warning("feed search failed: %s", type(e).__name__)
    if len(good()) < ENOUGH:
        try:
            found += news(query, limit)
        except Exception as e:  # noqa: BLE001
            logger.warning("news fallback failed: %s", type(e).__name__)
    if len(good()) < ENOUGH:
        url = f"https://www.bing.com/search?q={quote_plus(query)}&setlang=en&cc={_market().split('-')[-1]}"
        try:
            b = browser()
            found += parse_bing_html(b.call(b.html_of, url), limit)
        except Exception as e:  # noqa: BLE001
            logger.warning("browser search failed: %s", type(e).__name__)
    return good()[:limit]


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
    results = reachable(parse_rss(fetch(url).body, limit))
    return sorted(results, key=lambda r: r.date, reverse=True)


def render(results: list[Result]) -> str:
    if not results:
        return "No results."
    return "\n".join(
        f"{i}. {r.title}" + (f" ({r.date})" if r.date else "") + f"\n   {r.url}" + (f"\n   {r.snippet}" if r.snippet else "")
        for i, r in enumerate(results, 1)
    )


# ---- research papers ----
def _arxiv(query: str, limit: int) -> list[Result]:
    terms = "+AND+".join(f"all:{quote_plus(w)}" for w in sorted(_words(query))[:8]) or f"all:{quote_plus(query)}"
    body = fetch(f"https://export.arxiv.org/api/query?search_query={terms}&max_results={limit}&sortBy=relevance").body
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for entry in ET.fromstring(body).findall("a:entry", ns):
        title = " ".join((entry.findtext("a:title", "", ns)).split())
        link = entry.findtext("a:id", "", ns).strip()
        authors = ", ".join(a.findtext("a:name", "", ns) for a in entry.findall("a:author", ns)[:3])
        abstract = " ".join(entry.findtext("a:summary", "", ns).split())
        date = entry.findtext("a:published", "", ns)[:10]
        if title and link:
            out.append(Result(f"{title} (arXiv; {authors})", link, abstract[:300], date))
    return out


def _crossref(query: str, limit: int) -> list[Result]:
    import json

    data = json.loads(fetch(
        f"https://api.crossref.org/works?query={quote_plus(query)}&rows={limit}"
        "&select=DOI,title,author,container-title,issued,abstract,type"
    ).body)
    out = []
    for item in data.get("message", {}).get("items", []):
        title = " ".join((item.get("title") or [""])[0].split())
        if not title or not item.get("DOI"):
            continue
        parts = (item.get("issued", {}).get("date-parts") or [[None]])[0]
        year = str(parts[0]) if parts and parts[0] else ""
        venue = (item.get("container-title") or [""])[0]
        authors = ", ".join(f"{a.get('family', '')}" for a in (item.get("author") or [])[:3])
        abstract = re.sub(r"<[^>]+>", " ", item.get("abstract") or "")
        out.append(Result(f"{title} ({venue or item.get('type', '')}; {authors})", f"https://doi.org/{item['DOI']}",
                          " ".join(abstract.split())[:300], year))
    return out


def _pubmed(query: str, limit: int) -> list[Result]:
    import json

    ids = json.loads(fetch(
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term={quote_plus(query)}"
        f"&retmode=json&retmax={limit}&sort=relevance"
    ).body)["esearchresult"].get("idlist", [])
    if not ids:
        return []
    data = json.loads(fetch(
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed&id={','.join(ids)}&retmode=json"
    ).body)["result"]
    out = []
    for pid in ids:
        item = data.get(pid, {})
        authors = ", ".join(a.get("name", "") for a in item.get("authors", [])[:3])
        out.append(Result(f"{item.get('title', '').strip()} ({item.get('fulljournalname', '')}; {authors})",
                          f"https://pubmed.ncbi.nlm.nih.gov/{pid}/", "", (item.get("pubdate") or "")[:4]))
    return out


SCHOLARLY = {"arxiv": _arxiv, "crossref": _crossref, "pubmed": _pubmed}


def scholarly(query: str, limit: int = 6, sources: tuple[str, ...] = ("arxiv", "crossref", "pubmed")) -> list[Result]:
    """Research papers from several free indexes, most relevant first, at most `limit` each."""
    from concurrent.futures import ThreadPoolExecutor

    # Years the model adds ("2024 2025 2026") make arXiv require them as words and find nothing.
    query = " ".join(re.sub(r"\b(19|20)\d{2}\b", " ", clean_query(query)).split()) or query

    def one(name: str) -> list[Result]:
        try:
            return SCHOLARLY[name](query, limit)
        except Exception as e:  # noqa: BLE001 - one index down must not sink the rest
            logger.warning("%s search failed: %s", name, type(e).__name__)
            return []

    with ThreadPoolExecutor(len(sources)) as pool:
        found = [r for batch in pool.map(one, sources) for r in batch]
    return _dedupe(relevant(query, found))[: limit * 2]
