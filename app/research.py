"""Research across many websites at once, into one merged, ranked table.

For requests like "find all the new and interesting Android apps people are talking about, with what
makes each special, in a sheet". A chat turn can open a handful of pages; this reads dozens:

1. Find sources everywhere at once: forums (Hacker News incl. "Show HN" launches, Reddit, Lemmy, any
   other forum or site the person names), GitHub, web search (several phrasings) and news.
2. Rank them (discussion size, how many searches found them) and read the best ``max_sources``,
   several at a time, with the right reader for each site (thread APIs with comments, or the
   general page reader with its browser fallback for everything else).
3. The model pulls the matching records out of each source (``bigsummary``'s row extraction,
   the same one used for big files and mail sheets).
4. Records naming the same thing are merged across sources; the table is ranked by how many
   independent sources mention each one, and every row keeps its source links.

Runs as a background job; nothing here keeps page text beyond the job.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from app.browsing import community

logger = logging.getLogger(__name__)

MAX_SOURCES = 60
DEFAULT_SOURCES = 25
PARALLEL_READS = 4
SOURCE_CHARS = 20_000         # of each page/thread given to the model
QUERY_VARIANTS = ("{t}", "{t} recommendations", "{t} list", "{t} reddit")


@dataclass
class Source:
    url: str
    title: str
    kind: str                  # forum, github, web, news, listing
    weight: float = 0.0
    text: str = ""
    error: str = ""


@dataclass
class Report:
    columns: list[str]
    rows: list[dict]
    read: list[Source] = field(default_factory=list)
    failed: list[Source] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _key(name: str) -> str:
    """'Gander - offline file viewer' and 'gander' are the same thing."""
    first = re.split(r"\s+[-–—:|(]\s*|\s*\(", str(name).strip(), maxsplit=1)[0]
    return re.sub(r"[^a-z0-9]+", "", first.lower())


_EMPTY = {"none", "null", "n/a", "na", "unknown", "-", "not mentioned", "not specified"}


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip().lower() in _EMPTY | {""})


def choose(sources: list[Source], n: int, pick: Callable[[str], str] | None) -> list[Source]:
    """The n sources most worth reading. Popularity alone picks big news threads that mention the
    topic without naming any items, so the model first picks from the titles (one short call);
    the rest are filled in by weight. Without a model, or when its answer is unusable, by weight."""
    if pick is None or len(sources) <= n:
        return sources[:n]
    pool = sources[:n * 3]
    listing = "\n".join(f"{i}. [{s.kind}] {s.title[:120]} ({s.url[:80]})" for i, s in enumerate(pool))
    try:
        reply = pick(listing)
        picked = [int(x) for x in re.findall(r"\d+", reply.split("\n\n")[0])]
    except Exception as e:  # noqa: BLE001
        logger.warning("source choice failed: %s", type(e).__name__)
        return sources[:n]
    order = list(dict.fromkeys(i for i in picked if 0 <= i < len(pool)))[:n]
    if len(order) < n // 2:
        return sources[:n]
    rest = [s for i, s in enumerate(sources) if i not in order]
    return [pool[i] for i in order] + rest[:n - len(order)]


def merge(rows: list[dict], name_col: str, columns: list[str]) -> list[dict]:
    """One row per thing: the fullest value of each column, every source, how many sources."""
    merged: dict[str, dict] = {}
    for r in rows:
        k = _key(r.get(name_col, ""))
        if not k:
            continue
        cur = merged.setdefault(k, {c: "" for c in columns} | {"_sources": []})
        for c in columns:
            v = r.get(c, "")
            if not _blank(v) and len(str(v)) > len(str(cur.get(c) or "")):
                cur[c] = v
        src = r.get("_source", "")
        if src and src not in cur["_sources"]:
            cur["_sources"].append(src)
    out = []
    for m in merged.values():
        srcs = m.pop("_sources")
        out.append({**m, "Mentions": len(srcs), "Sources": "\n".join(srcs[:10])})
    return sorted(out, key=lambda r: -r["Mentions"])


def find_sources(topic: str, sites: tuple[str, ...], period: str, *, web: Callable[[str], list],
                 news: Callable[[str], list] | None, reddit_creds: dict | None, gh_token: str | None,
                 github_created_after: str = "") -> tuple[list[Source], list[Source], list[str]]:
    """(pages/threads to read, result listings that are records themselves, notes on what failed)."""
    found: dict[str, Source] = {}
    listings: list[Source] = []
    notes: list[str] = []

    def add(url: str, title: str, kind: str, weight: float) -> None:
        if not url.startswith(("http://", "https://")):
            return
        key = url.split("#")[0].rstrip("/")
        if key in found:
            found[key].weight += weight + 1  # found again by another search: more likely to matter
        else:
            found[key] = Source(url, title, kind, weight)

    forum_sites = tuple(s for s in sites if s not in ("web", "news", "github"))

    def forums() -> None:
        posts, n = community.search_all(topic, forum_sites, period, False, "", 15, reddit_creds, web)
        launches, n2 = community.search_all(topic, ("hackernews",), period, True, "", 15) \
            if "hackernews" in forum_sites else ([], [])
        notes.extend(n + n2)
        for p in posts + launches:
            add(p.discussion, p.title, "forum", 2 + min(10, (p.score or 0) / 50 + (p.comments or 0) / 20))
        if posts or launches:
            listings.append(Source("forum search results", "Forum posts", "listing", 0,
                                   community.render_posts(posts + launches, [])))

    def github() -> None:
        repos = community.github(topic, github_created_after, 20, "stars", 30, gh_token)
        if repos:
            listings.append(Source("https://github.com/search", "GitHub repositories", "listing", 0,
                                   community.render_repos(repos)))
            for r in repos[:5]:
                add(r.url, r.name, "github", 1 + min(5, r.stars / 500))

    def websearch() -> None:
        for v in QUERY_VARIANTS:
            for i, r in enumerate(web(v.format(t=topic))[:8]):
                add(r.url, r.title, "web", 3 - i * 0.3)

    def newssearch() -> None:
        for i, r in enumerate((news or (lambda q: []))(topic)[:8]):
            add(r.url, r.title, "news", 1.5 - i * 0.1)

    jobs = [("forums", forums)] if forum_sites else []
    if "github" in sites:
        jobs.append(("github", github))
    if "web" in sites:
        jobs.append(("web search", websearch))
    if "news" in sites and news is not None:
        jobs.append(("news", newssearch))

    def safe(job: tuple[str, Callable[[], None]]) -> None:
        try:
            job[1]()
        except Exception as e:  # noqa: BLE001 - one source down must not sink the rest
            logger.warning("%s failed: %s", job[0], type(e).__name__)
            notes.append(f"{job[0]}: failed ({type(e).__name__})")

    with ThreadPoolExecutor(max(1, len(jobs))) as pool:
        list(pool.map(safe, jobs))
    ranked = sorted(found.values(), key=lambda s: -s.weight)
    return ranked, listings, notes


def collect(topic: str, what: str, columns: list[str], sources: list[Source], listings: list[Source],
            reader: Callable[[str], str], extract: Callable[[str, str], list[dict]],
            progress: Callable[[str], None], max_sources: int = DEFAULT_SOURCES,
            pick: Callable[[str], str] | None = None) -> Report:
    """Read the best sources, extract records from each (and from the listings), merge."""
    progress("Choosing the most useful sources")
    chosen = choose(sources, max(1, min(max_sources, MAX_SOURCES)), pick)
    done = [0]

    def read(s: Source) -> Source:
        try:
            text = reader(s.url)
            if text.startswith("ERROR"):
                s.error = text[:200]
            else:
                s.text = text[:SOURCE_CHARS]
        except Exception as e:  # noqa: BLE001
            s.error = f"{type(e).__name__}"
        done[0] += 1
        progress(f"Read {done[0]} of {len(chosen)} sources")
        return s

    with ThreadPoolExecutor(PARALLEL_READS) as pool:
        got = list(pool.map(read, chosen))
    ok = [s for s in got if s.text] + [s for s in listings if s.text]
    report = Report(columns, [], [s for s in got if s.text], [s for s in got if not s.text])
    rows: list[dict] = []
    for i, s in enumerate(ok, 1):
        progress(f"Picking out {what or 'items'}: source {i} of {len(ok)}")
        try:
            for r in extract(s.text, s.url):
                link = str(r.get("Source") or r.get("Link") or "").strip()
                r["_source"] = s.url if s.kind != "listing" or not link.startswith("http") else link
                rows.append(r)
        except Exception as e:  # noqa: BLE001 - one unreadable reply must not sink the rest
            logger.warning("extraction failed: %s", type(e).__name__)
            report.notes.append(f"couldn't read the model's table for {s.url}")
    report.rows = merge(rows, columns[0], columns)
    report.columns = [*columns, "Mentions", "Sources"]
    return report
