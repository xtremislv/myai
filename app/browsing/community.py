"""What people are discussing and launching: forums and code hosts, through their public APIs.

General web search from this server is weak for "what do people recommend / what's new" questions
(keyless engines rate-limit it, and Reddit refuses its JSON and HTML pages to automated clients).
These sources answer such questions directly, with scores, comment counts and dates:

- Hacker News: the Algolia API (stories, "Show HN" launches, threads with all comments). No key.
- Reddit: with a ``reddit/oauth`` vault secret (an app the owner registers at reddit.com/prefs/apps,
  JSON ``{"client_id": ..., "client_secret": ...}``) the official API, with scores. Without one,
  Reddit's public RSS feeds (no scores), one request at a time with a pause between them, cached,
  and a plain "rate-limited, try later" when Reddit says 429. We never retry around a limit.
- Lemmy (lemmy.world, a federated Reddit-like forum): its public API. No key.
- GitHub: the repository search API (new or popular open-source projects, stars, topics) and
  READMEs. A ``github/token`` vault secret raises the rate limit (10 -> 30 searches a minute).
- Any other site: Discourse forums (thousands of product and community forums) through their public
  JSON; everything else through a web search limited to the site, and read as an ordinary page
  (``read`` raises NoAdapter, and the caller uses the general page reader with its browser fallback).

Measured through the proxy on 2026-11-05: Algolia, GitHub, Lemmy and Reddit RSS answer; Reddit
JSON/HTML answer 403; Reddit RSS answers 429 to a second request sent straight after the first.
"""
from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode, urlsplit

from app.browsing.fetch import fetch, post_form

logger = logging.getLogger(__name__)

API_AGENT = "linux:tw-ai-assistant:1.0 (self-hosted company assistant)"  # Reddit's API asks for one like this
PERIODS = {"day": 1, "week": 7, "month": 31, "year": 366, "all": 0}
SITES = ("hackernews", "reddit", "lemmy")


class RateLimited(RuntimeError):
    """The site asked us to slow down. The message is safe to show the model."""


@dataclass(frozen=True)
class Post:
    site: str
    title: str
    discussion: str          # the thread
    link: str = ""           # what the post links to (the app, repo or article), if not itself
    score: int | None = None
    comments: int | None = None
    date: str = ""
    community: str = ""
    snippet: str = ""


@dataclass(frozen=True)
class Repo:
    name: str
    url: str
    description: str
    stars: int
    created: str
    pushed: str
    language: str = ""
    topics: tuple[str, ...] = ()
    homepage: str = ""
    license: str = ""


@dataclass
class Thread:
    site: str
    title: str
    url: str
    text: str = ""
    link: str = ""
    score: int | None = None
    comments: list[tuple[str, str]] = field(default_factory=list)  # (meta, text)


# ---- helpers ----
_TAGS = re.compile(r"<[^>]+>")
_FILLER = {
    "new", "newest", "latest", "best", "top", "good", "great", "cool", "awesome", "underrated", "hidden", "gem", "gems",
    "useful", "interesting", "recommend", "recommended", "recommendations", "suggestions", "list", "some", "any",
    "find", "me", "show", "the", "a", "an", "of", "for", "to", "and", "or", "in", "on", "with", "that", "are", "is",
    "what", "which", "you", "your", "my", "i", "reddit", "threads", "thread", "discussion", "discussions", "public",
}


def plain(text: str | None, limit: int = 0) -> str:
    out = " ".join(html.unescape(_TAGS.sub(" ", text or "")).split())
    return out[:limit].rstrip() + "…" if limit and len(out) > limit else out


def keywords(query: str) -> list[str]:
    """The words that carry meaning. Forum search engines need every word to match, so the model's
    "new awesome underrated android apps 2026" finds nothing while "android apps" finds plenty."""
    words = re.findall(r"[\w+#.-]+", re.sub(r"\b(19|20)\d{2}\b", " ", query.lower()))
    kept = [w.strip(".-") for w in words if w.strip(".-") and w not in _FILLER]
    return kept or [w for w in words if w][:3]


def since(period: str) -> datetime | None:
    days = PERIODS.get((period or "year").strip().lower(), 366)
    return datetime.now(UTC) - timedelta(days=days) if days else None


def _json(url: str, headers: dict[str, str] | None = None) -> dict | list:
    page = fetch(url, headers)
    if page.status == 429:
        raise RateLimited(f"{urlsplit(url).hostname} is rate-limiting this server; try again in a minute")
    if page.status in (401, 403):
        raise PermissionError(f"{urlsplit(url).hostname} refused the request ({page.status})")
    if page.status >= 400:
        raise RuntimeError(f"{urlsplit(url).hostname} answered {page.status}")
    return json.loads(page.body)


def _day(ts: str | int | float | None) -> str:
    if ts in (None, ""):
        return ""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")
    return str(ts)[:10]


# ---- Hacker News (Algolia) ----
def _hn_query(words: list[str], period: str, launches: bool, limit: int, optional: bool) -> list[Post]:
    params = {"query": " ".join(words), "tags": "show_hn" if launches else "story", "hitsPerPage": str(max(limit * 2, 20))}
    start = since(period)
    if start:
        params["numericFilters"] = f"created_at_i>{int(start.timestamp())}"
    if optional:
        params["optionalWords"] = " ".join(words)
    data = _json("https://hn.algolia.com/api/v1/search?" + urlencode(params))
    out = []
    for h in data.get("hits", []):
        disc = f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        out.append(Post("Hacker News", h.get("title") or "", disc, h.get("url") or "", h.get("points"),
                        h.get("num_comments"), _day(h.get("created_at")), "Show HN" if launches else "",
                        plain(h.get("story_text"), 240)))
    return out


def hackernews(query: str, period: str = "year", launches: bool = False, limit: int = 10) -> list[Post]:
    words = keywords(query)
    found = _hn_query(words, period, launches, limit, optional=False)
    if len(found) < 3 and len(words) > 1:  # loosen: rank by how many words match instead of requiring all
        seen = {p.discussion for p in found}
        found += [p for p in _hn_query(words, period, launches, limit, optional=True) if p.discussion not in seen]
    return sorted(found, key=lambda p: -(p.score or 0))[:limit]


def _hn_comments(node: dict, depth: int, out: list[tuple[str, str]], limit: int) -> None:
    for child in node.get("children") or []:
        if len(out) >= limit:
            return
        text = plain(child.get("text"), 700)
        if text and text not in ("[flagged]", "[dead]", "[deleted]"):
            out.append((f"{'  ' * depth}{child.get('author') or '?'}", text))
        if depth < 1:
            _hn_comments(child, depth + 1, out, limit)


def hn_thread(item_id: str, limit: int = 40) -> Thread:
    data = _json(f"https://hn.algolia.com/api/v1/items/{int(item_id)}")
    t = Thread("Hacker News", data.get("title") or "", f"https://news.ycombinator.com/item?id={item_id}",
               plain(data.get("text"), 3000), data.get("url") or "", data.get("points"))
    _hn_comments(data, 0, t.comments, limit)
    return t


# ---- Reddit ----
REDDIT_GAP = 6.0  # seconds between our Reddit requests (it refuses a second one sent straight away)
CACHE_SECONDS = 900
CACHE_ENTRIES = 300
_reddit_lock = threading.Lock()
_reddit_last = [0.0]
_cache: dict[str, tuple[float, str]] = {}
_token: dict[str, tuple[float, str]] = {}


def _reddit_get(url: str, headers: dict[str, str] | None = None) -> str:
    """One Reddit request at a time, REDDIT_GAP apart, answers cached. A 429 is reported, not retried."""
    now = time.monotonic()
    hit = _cache.get(url)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    with _reddit_lock:
        wait = REDDIT_GAP - (time.monotonic() - _reddit_last[0])
        if wait > 0:
            time.sleep(wait)
        try:
            page = fetch(url, headers)
        finally:
            _reddit_last[0] = time.monotonic()
    if page.status == 429:
        raise RateLimited("Reddit is rate-limiting this server right now; try Reddit again in a few minutes "
                          "(Hacker News, Lemmy and GitHub are unaffected)")
    if page.status in (401, 403):
        raise PermissionError(f"Reddit refused the request ({page.status})")
    if page.status >= 400:
        raise RuntimeError(f"Reddit answered {page.status}")
    if len(_cache) >= CACHE_ENTRIES:  # drop the oldest half: a cache, not a store
        for old in sorted(_cache, key=lambda k: _cache[k][0])[:CACHE_ENTRIES // 2]:
            _cache.pop(old, None)
    _cache[url] = (time.monotonic(), page.body)
    return page.body


def _reddit_token(creds: dict) -> str:
    key = creds["client_id"]
    hit = _token.get(key)
    if hit and hit[0] > time.time() + 60:
        return hit[1]
    data = post_form("https://www.reddit.com/api/v1/access_token", {"grant_type": "client_credentials"},
                     {"User-Agent": API_AGENT}, auth=(creds["client_id"], creds["client_secret"]))
    _token[key] = (time.time() + float(data.get("expires_in", 3600)), data["access_token"])
    return data["access_token"]


_ATOM = {"a": "http://www.w3.org/2005/Atom"}


def parse_reddit_feed(xml_text: str) -> list[dict]:
    """Entries of a Reddit Atom feed: title, link, date, author, community, text (HTML stripped)."""
    root = ET.fromstring(xml_text)
    out = []
    for e in root.findall("a:entry", _ATOM):
        link = e.find("a:link", _ATOM)
        cat = e.find("a:category", _ATOM)
        body = e.findtext("a:content", "", _ATOM)
        outside = re.search(r'href="(https?://(?!www\.reddit\.com|i\.redd\.it|preview\.redd\.it)[^"]+)"[^>]*>\s*\[link\]', body)
        out.append({
            "title": html.unescape(e.findtext("a:title", "", _ATOM)).strip(),
            "url": link.get("href", "") if link is not None else "",
            "date": (e.findtext("a:published", "", _ATOM) or e.findtext("a:updated", "", _ATOM))[:10],
            "author": (e.findtext("a:author/a:name", "", _ATOM) or "").removeprefix("/u/"),
            "community": f"r/{cat.get('term')}" if cat is not None and cat.get("term") else "",
            "link": html.unescape(outside.group(1)) if outside else "",
            "text": plain(re.sub(r"submitted by .*$", "", body, flags=re.S)),
        })
    return out


def _reddit_period(period: str) -> str:
    p = (period or "year").lower()
    return p if p in ("day", "week", "month", "year", "all") else "year"


def reddit(query: str, period: str = "year", subreddit: str = "", limit: int = 10, creds: dict | None = None) -> list[Post]:
    sub = re.sub(r"[^\w]", "", subreddit.removeprefix("r/").removeprefix("/r/"))
    t = _reddit_period(period)
    q = " ".join(keywords(query))
    if creds:
        token = _reddit_token(creds)
        path = f"/r/{sub}/search" if sub else "/search"
        params = {"q": q, "sort": "top", "t": t, "limit": str(limit), "raw_json": "1"}
        if sub:
            params["restrict_sr"] = "1"
        data = json.loads(_reddit_get(f"https://oauth.reddit.com{path}?" + urlencode(params),
                                      {"Authorization": f"bearer {token}", "User-Agent": API_AGENT}))
        out = []
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            link = d.get("url") or ""
            out.append(Post("Reddit", d.get("title", ""), f"https://www.reddit.com{d.get('permalink', '')}",
                            "" if "reddit.com" in link else link, d.get("score"), d.get("num_comments"),
                            _day(d.get("created_utc")), f"r/{d.get('subreddit', '')}", plain(d.get("selftext"), 240)))
        return out
    if q:
        base = f"https://www.reddit.com/r/{sub}/search.rss" if sub else "https://www.reddit.com/search.rss"
        params = {"q": q, "sort": "top", "t": t, "limit": str(limit)}
        if sub:
            params["restrict_sr"] = "on"
        url = base + "?" + urlencode(params)
    elif sub:
        url = f"https://www.reddit.com/r/{sub}/top/.rss?t={t}&limit={limit}"
    else:
        return []
    return [Post("Reddit", e["title"], e["url"], e["link"], None, None, e["date"], e["community"], plain(e["text"], 240))
            for e in parse_reddit_feed(_reddit_get(url))][:limit]


def reddit_thread(url: str, limit: int = 40, creds: dict | None = None) -> Thread:
    parts = urlsplit(url)
    m = re.search(r"/comments/([a-z0-9]+)", parts.path)
    if not m:
        raise ValueError("not a Reddit thread link (it needs /comments/<id>/)")
    if creds:
        token = _reddit_token(creds)
        data = json.loads(_reddit_get(
            f"https://oauth.reddit.com/comments/{m.group(1)}?sort=top&limit={limit}&depth=2&raw_json=1",
            {"Authorization": f"bearer {token}", "User-Agent": API_AGENT}))
        post = data[0]["data"]["children"][0]["data"]
        t = Thread("Reddit", post.get("title", ""), url, plain(post.get("selftext"), 3000),
                   "" if "reddit.com" in (post.get("url") or "") else post.get("url", ""), post.get("score"))

        def walk(children: list, depth: int) -> None:
            for c in children:
                d = c.get("data", {})
                if c.get("kind") != "t1" or len(t.comments) >= limit:
                    continue
                t.comments.append((f"{'  ' * depth}{d.get('author')} ({d.get('score')} points)", plain(d.get("body"), 700)))
                replies = d.get("replies")
                if depth < 1 and isinstance(replies, dict):
                    walk(replies.get("data", {}).get("children", []), depth + 1)

        walk(data[1]["data"]["children"], 0)
        return t
    path = parts.path.rstrip("/")
    entries = parse_reddit_feed(_reddit_get(f"https://www.reddit.com{path}/.rss?sort=top&limit={limit}"))
    if not entries:
        raise RuntimeError("Reddit returned an empty thread")
    head, rest = entries[0], entries[1:]
    t = Thread("Reddit", head["title"], url, plain(head["text"], 3000), head["link"])
    t.comments = [(e["author"] or "?", plain(e["text"], 700)) for e in rest[:limit] if e["text"]]
    return t


# ---- Lemmy ----
LEMMY = "https://lemmy.world"
_LEMMY_SORT = {"day": "TopDay", "week": "TopWeek", "month": "TopMonth", "year": "TopYear", "all": "TopAll"}


def _lemmy_query(q: str, period: str, limit: int) -> list[Post]:
    params = {"q": q, "type_": "Posts", "sort": _LEMMY_SORT.get((period or "year").lower(), "TopYear"), "limit": str(limit)}
    data = _json(f"{LEMMY}/api/v3/search?" + urlencode(params))
    out = []
    for p in data.get("posts", []):
        post, counts, comm = p.get("post", {}), p.get("counts", {}), p.get("community", {})
        out.append(Post("Lemmy", post.get("name", ""), f"{LEMMY}/post/{post.get('id')}", post.get("url") or "",
                        counts.get("score"), counts.get("comments"), _day(post.get("published")),
                        f"c/{comm.get('name', '')}", plain(post.get("body"), 240)))
    return out


def lemmy(query: str, period: str = "year", limit: int = 10) -> list[Post]:
    words = keywords(query)
    found = _lemmy_query(" ".join(words), period, limit)
    if len(found) < 3 and len(words) > 2:  # Lemmy matches the words as one phrase: try the first two
        seen = {p.discussion for p in found}
        found += [p for p in _lemmy_query(" ".join(words[:2]), period, limit) if p.discussion not in seen]
    return sorted(found, key=lambda p: -(p.score or 0))[:limit]


def lemmy_thread(url: str, limit: int = 40) -> Thread:
    m = re.search(r"/post/(\d+)", urlsplit(url).path)
    if not m:
        raise ValueError("not a Lemmy post link (it needs /post/<id>)")
    base = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
    post = _json(f"{base}/api/v3/post?id={m.group(1)}").get("post_view", {})
    t = Thread("Lemmy", post.get("post", {}).get("name", ""), url, plain(post.get("post", {}).get("body"), 3000),
               post.get("post", {}).get("url") or "", post.get("counts", {}).get("score"))
    data = _json(f"{base}/api/v3/comment/list?" + urlencode({"post_id": m.group(1), "sort": "Top", "limit": str(limit), "max_depth": "2", "type_": "All"}))
    for c in data.get("comments", [])[:limit]:
        depth = c.get("comment", {}).get("path", "0.x").count(".") - 1
        t.comments.append((f"{'  ' * max(0, depth)}{c.get('creator', {}).get('name', '?')} ({c.get('counts', {}).get('score')} points)",
                           plain(c.get("comment", {}).get("content"), 700)))
    return t


# ---- GitHub ----
def _gh_headers(token: str | None) -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def github(query: str, created_after: str = "", min_stars: int = 0, sort: str = "stars", limit: int = 15,
           token: str | None = None) -> list[Repo]:
    """Repositories matching words and GitHub qualifiers (topic:android, language:kotlin, ...)."""
    qualifiers = [w for w in query.split() if ":" in w]
    words = keywords(" ".join(w for w in query.split() if ":" not in w))
    parts = qualifiers + words
    if created_after and re.fullmatch(r"\d{4}-\d{2}-\d{2}", created_after.strip()):
        parts.append(f"created:>={created_after.strip()}")
    if min_stars > 0:
        parts.append(f"stars:>={int(min_stars)}")
    order = sort if sort in ("stars", "updated", "forks") else "stars"
    url = "https://api.github.com/search/repositories?" + urlencode(
        {"q": " ".join(parts), "sort": order, "order": "desc", "per_page": str(min(max(limit, 1), 50))})
    data = _json(url, _gh_headers(token))
    return [Repo(r["full_name"], r["html_url"], r.get("description") or "", r.get("stargazers_count", 0),
                 _day(r.get("created_at")), _day(r.get("pushed_at")), r.get("language") or "",
                 tuple(r.get("topics") or ())[:8], r.get("homepage") or "",
                 ((r.get("license") or {}).get("spdx_id") or "")) for r in data.get("items", [])]


def github_readme(url: str, token: str | None = None) -> Thread:
    m = re.match(r"/([\w.-]+)/([\w.-]+)", urlsplit(url).path)
    if not m:
        raise ValueError("not a GitHub repository link")
    owner, name = m.group(1), m.group(2).removesuffix(".git")
    repo = _json(f"https://api.github.com/repos/{quote(owner)}/{quote(name)}", _gh_headers(token))
    page = fetch(f"https://api.github.com/repos/{quote(owner)}/{quote(name)}/readme",
                 {**_gh_headers(token), "Accept": "application/vnd.github.raw+json"})
    readme = page.body if page.status == 200 else f"(no README: GitHub answered {page.status})"
    readme = re.sub(r"\n{3,}", "\n\n", html.unescape(_TAGS.sub(" ", re.sub(r"!\[[^\]]*\]\([^)]*\)", "", readme))))
    head = (f"{repo.get('description') or ''}\nStars: {repo.get('stargazers_count')} · Created {_day(repo.get('created_at'))}"
            f" · Last push {_day(repo.get('pushed_at'))} · Language {repo.get('language') or '?'}"
            f" · Topics: {', '.join(repo.get('topics') or []) or '-'} · Homepage: {repo.get('homepage') or '-'}\n\n")
    return Thread("GitHub", repo.get("full_name", f"{owner}/{name}"), repo.get("html_url", url), head + readme[:12000],
                  repo.get("homepage") or "", repo.get("stargazers_count"))


# ---- any other site ----
class NoAdapter(ValueError):
    """No structured reader for this site: read it as an ordinary web page."""


_DOMAIN = re.compile(r"^(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)/?$", re.I)
_discourse: dict[str, bool] = {}  # domain -> runs Discourse (answers /search.json)


def domain_of(site: str) -> str | None:
    m = _DOMAIN.match(site.strip().lower())
    return m.group(1) if m else None


def discourse_search(domain: str, query: str, limit: int = 10) -> list[Post]:
    """Search a Discourse forum (thousands of product and community forums run it) through its
    public JSON. Raises NoAdapter when the site isn't Discourse."""
    if _discourse.get(domain) is False:
        raise NoAdapter(domain)
    page = fetch(f"https://{domain}/search.json?" + urlencode({"q": " ".join(keywords(query))}),
                 {"Accept": "application/json"})
    try:
        data = json.loads(page.body) if page.status == 200 else None
    except ValueError:
        data = None
    if len(_discourse) > 2000:
        _discourse.clear()
    if not isinstance(data, dict) or "topics" not in data and "posts" not in data:
        _discourse[domain] = False
        raise NoAdapter(domain)
    _discourse[domain] = True
    blurbs = {p.get("topic_id"): plain(p.get("blurb"), 240) for p in data.get("posts") or []}
    return [Post(domain, t.get("title") or t.get("fancy_title") or "", f"https://{domain}/t/{t.get('slug', 'topic')}/{t.get('id')}",
                 "", t.get("like_count"), max(0, (t.get("posts_count") or 1) - 1), _day(t.get("created_at")), "",
                 blurbs.get(t.get("id"), "")) for t in (data.get("topics") or [])[:limit]]


def discourse_thread(url: str, limit: int = 40) -> Thread:
    parts = urlsplit(url)
    m = re.match(r"(/t/[^/]+/\d+|/t/\d+)", parts.path)
    if not m:
        raise NoAdapter(url)
    page = fetch(f"{parts.scheme}://{parts.netloc}{m.group(1)}.json", {"Accept": "application/json"})
    try:
        data = json.loads(page.body) if page.status == 200 else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or "post_stream" not in data:
        raise NoAdapter(url)
    posts = data["post_stream"].get("posts") or []
    first = posts[0] if posts else {}
    t = Thread(parts.netloc, data.get("title", ""), url, plain(first.get("cooked"), 3000), "", data.get("like_count"))
    t.comments = [(f"{p.get('username', '?')}" + (f" ({p.get('reactions_count') or p.get('like_count') or 0} likes)"),
                   plain(p.get("cooked"), 700)) for p in posts[1:limit + 1] if p.get("cooked")]
    return t


def any_site(domain: str, query: str, limit: int, web_search: Callable[[str], list] | None) -> list[Post]:
    """Any website: its own forum search when it runs Discourse, else a web search limited to it."""
    try:
        return discourse_search(domain, query, limit)
    except NoAdapter:
        pass
    if web_search is None:
        return []
    return [Post(domain, r.title, r.url, "", None, None, r.date, "", r.snippet[:240])
            for r in web_search(f"{' '.join(keywords(query))} site:{domain}")[:limit]]


# ---- fan-out and rendering ----
ALIASES = {"hn": "hackernews", "ycombinator.com": "hackernews", "news.ycombinator.com": "hackernews",
           "reddit.com": "reddit", "lemmy.world": "lemmy", "github.com": "github"}


def search_all(query: str, sites: tuple[str, ...], period: str, launches: bool, subreddit: str, limit: int,
               reddit_creds: dict | None = None,
               web_search: Callable[[str], list] | None = None) -> tuple[list[Post], list[str]]:
    """Every site at once; a site that fails is named in the notes rather than sinking the rest.
    sites: the built-in names (hackernews, reddit, lemmy) and/or any domains (xda-developers.com,
    forum.example.org): Discourse forums are searched directly, other sites through web_search."""
    from concurrent.futures import ThreadPoolExecutor

    jobs: dict[str, Callable[[], list[Post]]] = {
        "hackernews": lambda: hackernews(query, period, launches, limit),
        "reddit": lambda: reddit(query, period, subreddit, limit, reddit_creds),
        "lemmy": lambda: lemmy(query, period, limit),
    }
    chosen = []
    for raw in sites:
        name = ALIASES.get(raw.strip().lower().removeprefix("www."), raw.strip().lower())
        if name in jobs:
            chosen.append(name)
        elif (dom := domain_of(name)) and dom not in jobs:
            jobs[dom] = (lambda d: lambda: any_site(d, query, limit, web_search))(dom)
            chosen.append(dom)
    chosen = list(dict.fromkeys(chosen))
    notes: list[str] = []

    def run(name: str) -> list[Post]:
        try:
            return jobs[name]()
        except (RateLimited, PermissionError) as e:
            notes.append(f"{name}: {e}")
        except Exception as e:  # noqa: BLE001 - one site down must not sink the rest
            logger.warning("%s search failed: %s", name, type(e).__name__)
            notes.append(f"{name}: could not be reached ({type(e).__name__})")
        return []

    with ThreadPoolExecutor(max(1, len(chosen))) as pool:
        found = [p for batch in pool.map(run, chosen) for p in batch]
    return found, notes


def render_posts(posts: list[Post], notes: list[str]) -> str:
    lines = []
    for i, p in enumerate(posts, 1):
        meta = [p.site, p.community] + ([f"{p.score} points"] if p.score is not None else []) \
            + ([f"{p.comments} comments"] if p.comments is not None else []) + ([p.date] if p.date else [])
        lines.append(f"{i}. {p.title} [{', '.join(m for m in meta if m)}]\n   thread: {p.discussion}"
                     + (f"\n   links to: {p.link}" if p.link else "") + (f"\n   {p.snippet}" if p.snippet else ""))
    if not lines:
        lines.append("No matching discussions.")
    if notes:
        lines.append("Not available this time: " + "; ".join(notes))
    return "\n".join(lines)


def render_repos(repos: list[Repo]) -> str:
    if not repos:
        return "No matching repositories."
    return "\n".join(
        f"{i}. {r.name} ★{r.stars} [{r.language or '?'}; created {r.created}; last push {r.pushed}"
        + (f"; {r.license}" if r.license else "") + f"]\n   {r.url}" + (f"  site: {r.homepage}" if r.homepage else "")
        + (f"\n   {r.description[:300]}" if r.description else "") + (f"\n   topics: {', '.join(r.topics)}" if r.topics else "")
        for i, r in enumerate(repos, 1))


def render_thread(t: Thread) -> str:
    unit = "stars" if t.site == "GitHub" else "points"
    out = [f"{t.title} ({t.site}" + (f", {t.score} {unit}" if t.score is not None else "") + f")\n{t.url}"]
    if t.link:
        out.append(f"Links to: {t.link}")
    if t.text:
        out.append(t.text)
    if t.comments:
        out.append(f"\nTop comments ({len(t.comments)}):")
        out += [f"- {meta}: {text}" for meta, text in t.comments]
    return "\n".join(out)


def read(url: str, limit: int = 40, reddit_creds: dict | None = None, github_token: str | None = None) -> Thread:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.").removeprefix("old.")
    if host == "news.ycombinator.com":
        m = re.search(r"id=(\d+)", url)
        if not m:
            raise ValueError("not a Hacker News item link (it needs ?id=<number>)")
        return hn_thread(m.group(1), limit)
    if host in ("reddit.com", "np.reddit.com", "new.reddit.com"):
        return reddit_thread(url, limit, reddit_creds)
    if host == "github.com":
        return github_readme(url, github_token)
    path = urlsplit(url).path
    if re.search(r"/post/\d+", path):
        try:
            return lemmy_thread(url, limit)
        except (ValueError, RuntimeError, PermissionError, KeyError):
            raise NoAdapter(url) from None
    if path.startswith("/t/"):
        return discourse_thread(url, limit)  # NoAdapter when the site isn't Discourse
    raise NoAdapter(url)
