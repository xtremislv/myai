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
# Interstitials that pass on their own once the site's script has looked at a real browser
# (AWS WAF's empty 202 page, Cloudflare's "just a moment"): worth waiting a few seconds for.
_PASSING_SIGNS = ("just a moment", "checking your browser", "please wait", "an empty page with no title")


def passing_check(title: str, text: str) -> bool:
    """True for a challenge page the browser itself usually passes in a few seconds; False for
    real content and for hard refusals ("Access Denied"), where waiting changes nothing."""
    why = block_reason(title, text)
    return bool(why) and any(s in why for s in _PASSING_SIGNS)


# What kind of refusal a block page is, for the advice the assistant gets (security of the next step:
# never retry or work around it; owner's rule: other ways around a block are suggested, not tried).
_CAPTCHA_SIGNS = ("captcha", "verify you are human", "verify you are a human", "are you a robot", "robot check",
                  "unusual traffic", "please complete the security check", "press & hold", "press and hold")
_BUSY_SIGNS = ("it's rush hour", "service unavailable error", "looks like all of india is here",
               "hang in there, the offers are worth the wait")
# A sign-in wall instead of the content (Instagram, TikTok, Facebook, LinkedIn show this to signed-out visitors).
_SIGN_IN_SIGNS = ("log in to continue", "login to continue", "sign in to continue", "log in to see",
                  "sign up to see", "log into instagram", "log in to instagram", "login • instagram",
                  "you must be logged in", "please log in to", "log in to tiktok", "log in to facebook",
                  "see photos and videos from friends")
SIGN_IN_PAGE_MAX_CHARS = 4000


def block_kind(title: str, text: str, status: int | None = None) -> tuple[str, str] | None:
    """("address" | "captcha" | "sign-in" | "busy", why) for a page that isn't the real content, or None.
    address: the site refuses this server's address (a person can't fix it from here); captcha: a
    human check; sign-in: the content is behind an account; busy: the site says to come back later."""
    why = block_reason(title, text)
    head = f"{title}\n{text[:800]}".lower()
    if why is None and len(text) <= SIGN_IN_PAGE_MAX_CHARS:
        sign = next((s for s in _SIGN_IN_SIGNS if s in head), None)
        if sign:
            return "sign-in", f"a sign-in page ('{sign}')"
    if why is None:
        if status in (401, 403):
            return "address", f"HTTP {status}"
        if status in (429, 503):
            return "busy", f"HTTP {status}"
        return None
    if any(s in head for s in _CAPTCHA_SIGNS):
        return "captcha", why
    if any(s in head for s in _BUSY_SIGNS) or status in (429, 503):
        return "busy", why
    return "address", why


_ADVICE = {
    "address": ("{site} refuses this server's address ({why}). Retrying, or a person solving something, won't "
                "change that. FIRST get the same information from another source yourself (see 'When a site is "
                "blocked'), then tell the person {site} was blocked and what you used instead, and offer {site}'s "
                "link so they can do it themselves."),
    "captcha": ("{site} shows a human check / CAPTCHA ({why}). Agents never solve these. FIRST try another source "
                "for the same thing. If only {site} will do: in the app, ask_person_to_browse; on WhatsApp, "
                "show_person with the screenshot and ask how to go on (they can't solve it from WhatsApp: offer "
                "the link, or the app)."),
    "sign-in": ("{site} only shows this to signed-in people ({why}). Agents never sign in for people. FIRST try "
                "another source. For an Instagram/TikTok/Facebook post or reel, ask the person to forward the "
                "video or screenshots to the WhatsApp bot: you can watch videos and read images sent there. In "
                "the app, ask_person_to_browse lets them sign in themselves; on WhatsApp, show_person can show "
                "them the page."),
    "busy": ("{site} says it's overloaded ({why}). Try another source now; {site} may work again later."),
}


def blocked_advice(site: str, kind: str, why: str) -> str:
    """The BLOCKED line a tool result starts with: what kind of block, and what to do next."""
    return f"BLOCKED ({kind}): " + _ADVICE.get(kind, _ADVICE["address"]).format(site=site, why=why)


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
