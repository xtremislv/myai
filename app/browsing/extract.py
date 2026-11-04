"""Readable text, links and tables out of HTML, sized for a small context window."""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

_NOISE = ("script", "style", "noscript", "svg", "iframe", "template", "canvas")
_CHROME = ("nav", "footer", "header", "aside", "form")
_SPACES = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n\s*\n+")


def soup_of(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_NOISE):
        tag.decompose()
    return soup


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + f" … [cut: {len(text) - limit} more characters]"


def _text(node) -> str:
    raw = node.get_text("\n")
    lines = (_SPACES.sub(" ", line).strip() for line in raw.split("\n"))
    return _BLANKS.sub("\n\n", "\n".join(line for line in lines if line)).strip()


def page_text(html: str) -> tuple[str, str]:
    """(title, main text). Prefers <main>/<article> when they hold most of the content."""
    soup = soup_of(html)
    title = _SPACES.sub(" ", soup.title.get_text()).strip() if soup.title else ""
    for tag in soup(_CHROME):
        tag.decompose()
    body = soup.body or soup
    text = _text(body)
    for main in soup.select("main, article, [role=main]"):
        main_text = _text(main)
        if len(main_text) > max(400, len(text) * 0.4):
            return title, main_text
    return title, text


def links(html: str, base_url: str, limit: int = 40) -> list[tuple[str, str]]:
    """(text, absolute url) pairs, deduplicated, in page order."""
    seen, out = set(), []
    for a in soup_of(html).select("a[href]"):
        href = urljoin(base_url, a["href"])
        if not href.startswith(("http://", "https://")) or href in seen:
            continue
        seen.add(href)
        label = _SPACES.sub(" ", a.get_text(" ")).strip()[:80] or a.get("title", "")[:80]
        out.append((label, href))
        if len(out) >= limit:
            break
    return out


def tables(html: str, limit: int = 3, max_rows: int = 25) -> list[str]:
    """The first tables as pipe-separated rows."""
    out = []
    for table in soup_of(html).find_all("table")[:limit]:
        rows = []
        for tr in table.find_all("tr")[:max_rows]:
            cells = [_SPACES.sub(" ", c.get_text(" ")).strip() for c in tr.find_all(["th", "td"])]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            out.append("\n".join(rows))
    return out


def select_text(html: str, selector: str, limit: int = 30) -> list[str]:
    """Text of every element matching a CSS selector."""
    out = []
    for node in soup_of(html).select(selector)[:limit]:
        text = _text(node)
        if text:
            out.append(text)
    return out


# Pages a site shows instead of its content when it refuses automated visitors. Only checked on
# short pages, so an article that merely mentions "access denied" is not mistaken for one.
_BLOCK_SIGNS = (
    "access denied", "request blocked", "the request could not be satisfied", "attention required",
    "just a moment", "checking your browser", "verify you are human", "verify you are a human",
    "are you a robot", "captcha", "unusual traffic", "pardon our interruption", "bot detection",
    "enable javascript and cookies", "403 forbidden", "forbidden", "you don't have permission",
    "please complete the security check", "request unsuccessful", "incapsula", "cloudflare ray id",
    "automated access to amazon data", "api-services-support@amazon.com", "robot check",
    # Amazon's answer to automated browsers: a "503 - Service Unavailable" page about rush hour.
    "it's rush hour and traffic is piling up", "service unavailable error",
    # Flipkart's: "Wow, looks like all of India is here! Hang in there... Retry Now" (an HTTP 429).
    "looks like all of india is here", "hang in there, the offers are worth the wait",
)
BLOCK_PAGE_MAX_CHARS = 1500


def block_reason(title: str, text: str) -> str | None:
    """Why this looks like a block or challenge page rather than the real content, or None."""
    if len(text) > BLOCK_PAGE_MAX_CHARS:
        return None
    head = f"{title}\n{text[:600]}".lower()
    sign = next((s for s in _BLOCK_SIGNS if s in head), None)
    if sign:
        return sign
    if title.strip().lower() == "error" or title.lower().startswith(("error |", "error -", "403", "404", "429", "503")):
        return f"an error page ('{title.strip()}')"
    if not title.strip() and len(text.strip()) < 150:
        return "an empty page with no title"
    return None
