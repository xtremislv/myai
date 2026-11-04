"""Product research on shopping sites: search results and product pages, as structured facts.

Pages are rendered in real Chromium (shops are JavaScript-heavy and block plain clients). Data
comes, in order of trust, from the page's own structured data (schema.org JSON-LD, which most
shops publish for search engines), from known layouts (Amazon), and from a generic reading of
"product cards": a link near a price.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import quote_plus, urljoin, urlsplit

from bs4 import BeautifulSoup

SITES = {
    "amazon": "https://www.amazon.in/s?k={q}",
    "flipkart": "https://www.flipkart.com/search?q={q}",
    "croma": "https://www.croma.com/searchB?q={q}%3Arelevance&text={q}",
    "reliancedigital": "https://www.reliancedigital.in/products?q={q}",
}
DEFAULT_SITES = ("amazon", "flipkart")
# Shops whose product pages and checkout the assistant's browser could open from this server
# (2026-11-04: Reliance Digital yes; Flipkart's search yes but its product pages answer 429; Amazon
# and Croma refuse). A blocked search falls back to the first of these that wasn't asked for.
WORKING_SITES = ("reliancedigital", "flipkart")
PRICE = re.compile(r"(?:₹|Rs\.?|INR)\s?([\d,]+(?:\.\d{1,2})?)")
MAX_RESULTS = 8


@dataclass
class Product:
    title: str
    url: str
    site: str
    price: str = ""
    rating: str = ""
    reviews: str = ""
    extra: dict = field(default_factory=dict)

    def line(self) -> str:
        bits = [f"{self.price}" if self.price else "price not shown",
                f"rating {self.rating}" + (f" ({self.reviews} reviews)" if self.reviews else "") if self.rating else ""]
        return f"{self.title} | {' | '.join(b for b in bits if b)} | {self.site}\n   {self.url}"


def _site_name(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.")


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def _money(value) -> str:
    if value in (None, ""):
        return ""
    text = str(value)
    if PRICE.search(text):
        return "₹" + PRICE.search(text).group(1)
    try:
        return f"₹{float(text):,.0f}"
    except ValueError:
        return text[:30]


# ---- structured data ----
def json_ld(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop(0)
            if isinstance(item, dict):
                out.append(item)
                stack += [v for k, v in item.items() if k in ("@graph", "itemListElement", "item") for v in (v if isinstance(v, list) else [v])]
    return out


def _from_ld(item: dict, base: str) -> Product | None:
    types = item.get("@type")
    types = types if isinstance(types, list) else [types]
    if "Product" not in types or not item.get("name"):
        return None
    offers = item.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    rating = item.get("aggregateRating") or {}
    price = offers.get("price") or offers.get("lowPrice") or ""
    extra = {}
    if offers.get("availability"):
        extra["availability"] = str(offers["availability"]).rsplit("/", 1)[-1]
    if isinstance(item.get("brand"), dict) and item["brand"].get("name"):
        extra["brand"] = item["brand"]["name"]
    if item.get("description"):
        extra["description"] = _clean(str(item["description"]))[:400]
    url = urljoin(base, item.get("url") or offers.get("url") or base)
    return Product(_clean(item["name"])[:160], url, _site_name(url), _money(price),
                   str(rating.get("ratingValue") or ""), str(rating.get("reviewCount") or rating.get("ratingCount") or ""), extra)


# ---- known layouts ----
def _amazon_results(soup: BeautifulSoup, base: str) -> list[Product]:
    out = []
    for card in soup.select('div[data-component-type="s-search-result"]'):
        title = card.select_one("h2 span") or card.select_one("h2")
        link = card.select_one("h2 a[href], a.a-link-normal.s-no-outline[href], a.a-link-normal[href*='/dp/']")
        if title is None or link is None:
            continue
        price = card.select_one("span.a-price span.a-offscreen")
        rating = card.select_one("span.a-icon-alt")
        reviews = card.select_one("span.a-size-base.s-underline-text, a[href*='customerReviews'] span")
        sponsored = "Sponsored" in card.get_text(" ")[:200]
        url = urljoin(base, link["href"]).split("/ref=")[0]
        out.append(Product(_clean(title.get_text())[:160], url, "amazon.in", _clean(price.get_text()) if price else "",
                           _clean(rating.get_text()).split(" out")[0] if rating else "",
                           _clean(reviews.get_text()).strip("()") if reviews else "",
                           {"sponsored": True} if sponsored else {}))
    return out


def _generic_results(soup: BeautifulSoup, base: str) -> list[Product]:
    """Product cards: the smallest block holding both a product link and a price."""
    out, seen = [], set()
    for node in soup.find_all(string=PRICE):
        card = node.parent
        for _ in range(7):
            if card is None or card.name in ("body", "html"):
                break
            links = [a for a in card.select("a[href]") if len(_clean(a.get_text()) or a.get("title", "")) > 12]
            if links and len(_clean(card.get_text())) < 700:
                link = max(links, key=lambda a: len(_clean(a.get_text()) or a.get("title", "")))
                url = urljoin(base, link["href"])
                if url in seen or not url.startswith("http"):
                    break
                seen.add(url)
                title = _clean(link.get("title") or link.get_text()) or _clean((card.select_one("img[alt]") or {}).get("alt", ""))
                title = re.sub(r"^(?:[A-Z][A-Z_]{3,}\s+)+", "", title)  # badges like LIMITED_TIME_OFFER
                title = PRICE.split(title, 1)[0].strip() if PRICE.search(title) else title  # price glued on
                text = _clean(card.get_text(" "))
                rating = re.search(r"\b([1-5](?:\.\d)?)\s?(?:★|out of 5|stars?)", text)
                out.append(Product(title[:160], url, _site_name(url), "₹" + PRICE.search(text).group(1),
                                   rating.group(1) if rating else ""))
                break
            card = card.parent
    return out


def parse_results(html: str, base: str) -> list[Product]:
    soup = BeautifulSoup(html, "lxml")
    found = [p for p in (_from_ld(i, base) for i in json_ld(html)) if p]
    if "amazon." in base:
        found += _amazon_results(soup, base)
    if len(found) < 3:
        found += _generic_results(soup, base)
    seen, out = set(), []
    for p in found:
        keys = {p.url.split("?")[0].split("/ref=")[0], p.title.lower()}
        if p.title and not keys & seen:
            seen |= keys
            out.append(p)
    return out


def parse_page(html: str, url: str) -> dict:
    """The facts of one product page."""
    soup = BeautifulSoup(html, "lxml")
    facts: dict = {"url": url, "site": _site_name(url)}
    for item in json_ld(html):
        p = _from_ld(item, url)
        if p:
            facts.update({"title": p.title, "price": p.price, "rating": p.rating, "reviews": p.reviews, **p.extra})
            break
    def text(sel: str) -> str:
        node = soup.select_one(sel)
        return _clean(node.get_text(" ")) if node else ""

    if "amazon." in url:
        facts.setdefault("title", text("#productTitle"))
        facts["price"] = facts.get("price") or text("#corePrice_feature_div span.a-offscreen") or text("span.a-price span.a-offscreen")
        facts["rating"] = facts.get("rating") or text("#acrPopover span.a-icon-alt").split(" out")[0]
        facts["reviews"] = facts.get("reviews") or text("#acrCustomerReviewText")
        facts["availability"] = facts.get("availability") or text("#availability")
        facts["seller"] = text("#merchantInfoFeature_feature_div .offer-display-feature-text, #sellerProfileTriggerId")
        facts["features"] = [_clean(li.get_text(" "))[:200] for li in soup.select("#feature-bullets li")][:8]
        facts["delivery"] = text("#mir-layout-DELIVERY_BLOCK, #deliveryBlockMessage")[:200]
    if not facts.get("title"):
        h1 = soup.select_one("h1")
        facts["title"] = _clean(h1.get_text(" ")) if h1 else _clean(soup.title.get_text() if soup.title else "")
    if not facts.get("price"):
        main = soup.select_one("main") or soup.body or soup
        match = PRICE.search(main.get_text(" ") if main else "")
        facts["price"] = "₹" + match.group(1) if match else ""
    if not facts.get("features"):
        facts["features"] = [_clean(li.get_text(" "))[:200] for li in soup.select("main li, [class*=highlight] li")][:6]
    return {k: v for k, v in facts.items() if v}


# ---- fetching (rendered) ----
def _render(url: str) -> str:
    from app.browsing.browser import browser

    b = browser()
    return b.call(b.html_of, url)


def search(query: str, sites: tuple[str, ...] = DEFAULT_SITES, render=None) -> tuple[list[Product], list[str]]:
    """(products, problems): the best matches from each site, interleaved."""
    from app.browsing.extract import block_reason, page_text

    render = render or _render
    per_site, problems = [], []
    for site in sites:
        template = SITES.get(site)
        if template is None:
            problems.append(f"{site}: not a known shop (known: {', '.join(SITES)})")
            continue
        url = template.format(q=quote_plus(query))
        try:
            html = render(url)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{site}: could not be reached ({type(e).__name__})")
            continue
        results = parse_results(html, url)  # real products outweigh any block-page heuristic
        if not results:
            title, body = page_text(html)
            if "chrome-error://" in html or re.search(r"ERR_[A-Z_]{4,}", html[:20000]):
                problems.append(f"{site}: can't be reached from this server's network")
            elif block_reason(title, body) or "api-services-support@amazon.com" in html:
                problems.append(f"{site}: refused automated access")
            else:
                problems.append(f"{site}: no products found on the results page")
            continue
        per_site.append(results[:MAX_RESULTS])
    merged = [p for group in zip(*[g + [None] * (MAX_RESULTS - len(g)) for g in per_site]) for p in group if p] if per_site else []
    return merged[: MAX_RESULTS * 2], problems


def details(url: str, render=None) -> dict:
    from app.browsing.extract import block_reason, page_text

    html = (render or _render)(url)
    facts = parse_page(html, url)
    title, body = page_text(html)
    if not facts.get("price") and (block_reason(title, body) or "api-services-support@amazon.com" in html):
        raise ValueError(f"{_site_name(url)} refused automated access to that page")
    return facts
