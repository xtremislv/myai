"""Gadget research across the brand stores the owner follows (2026-11-14), all at once.

Nearly all of them run on Shopify, which publishes product search and catalogues as public JSON
(`/search/suggest.json`, `/products.json`, `/products/<handle>.js`, the endpoints its own storefront
uses): fast, structured (price, MRP, stock, launch date) and no browser needed. Stores that aren't on
Shopify are searched through a site-limited web search instead.

Checked 2026-11-14 from this server: 19 stores answer; agaro.in has a broken certificate (its shop is
agarolifestyle.com), geeklane.com doesn't resolve (geeklane.in does), octor.in is now an unrelated blog
(left out), ugreen.com is the global store in US dollars (ugreenindia.com is the Indian one), and
dailyobjects.com draws results only for a real user (web search only). Shopify throttles a burst of
requests from one address (HTTP 429 "local_rate_limited") for a few minutes: answers are cached for 10
minutes and a throttled store is reported, never retried around.
"""
from __future__ import annotations

import html
import json
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import quote_plus, urlencode, urlsplit

from app.browsing.fetch import fetch
from app.config import get_settings

# name -> store address. "web:" marks a store searched through a site-limited web search.
DEFAULT_STORES = {
    "callmate": "https://callmateindia.com",
    "stuffcool": "https://www.stuffcool.com",
    "wecool": "https://www.wecool.in",
    "ultraprolink": "https://www.ultraprolink.com",
    "portronics": "https://www.portronics.com",
    "lifelong": "https://www.lifelongindiaonline.com",
    "agaro": "https://agarolifestyle.com",
    "bigsmall": "https://www.bigsmall.in",
    "atomberg": "https://atomberg.com",
    "geeklane": "https://www.geeklane.in",
    "sharge": "https://sharge.com",
    "boat": "https://www.boat-lifestyle.com",
    "noise": "https://www.gonoise.com",
    "zebronics": "https://zebronics.com",
    "ugreen": "https://www.ugreenindia.com",
    "ambrane": "https://ambraneindia.com",
    "conceptkart": "https://conceptkart.com",
    "instacuppa": "https://instacuppastore.com",
    "xech": "https://xech.com",
    "dailyobjects": "web:https://www.dailyobjects.com",
}
PER_STORE = 10
PARALLEL = 8
SORTS = ("relevance", "price_low", "price_high", "discount", "new")
CACHE_SECONDS = 600
_cache: OrderedDict[str, tuple[float, object]] = OrderedDict()
_lock = threading.Lock()


@dataclass
class StoreProduct:
    store: str
    title: str
    url: str
    price: float | None = None
    mrp: float | None = None
    available: bool | None = None
    kind: str = ""
    vendor: str = ""
    published: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def discount(self) -> int:
        if self.price and self.mrp and self.mrp > self.price:
            return round(100 * (self.mrp - self.price) / self.mrp)
        return 0

    def line(self) -> str:
        price = f"₹{self.price:,.0f}" if self.price else "price not shown"
        if self.discount:
            price += f" (MRP ₹{self.mrp:,.0f}, {self.discount}% off)"
        stock = "" if self.available is None else " | in stock" if self.available else " | OUT OF STOCK"
        new = f" | listed {self.published}" if self.published else ""
        kind = f" | {self.kind}" if self.kind else ""
        return f"{self.title} | {price}{stock}{kind}{new} | {self.store}\n   {self.url}"

    def row(self) -> dict:
        return {"Product": self.title, "Store": self.store, "Price (₹)": self.price or "", "MRP (₹)": self.mrp or "",
                "Discount %": self.discount or "", "In stock": "" if self.available is None else
                "yes" if self.available else "no", "Type": self.kind, "Listed": self.published, "Link": self.url}


def stores() -> dict[str, str]:
    """The stores to search: settings.gadget_stores ("name=url,...") if set, else DEFAULT_STORES."""
    raw = get_settings().gadget_stores.strip()
    if not raw:
        return dict(DEFAULT_STORES)
    out = {}
    for part in raw.split(","):
        name, _, url = part.strip().partition("=")
        if name and url:
            out[name.strip().lower()] = url.strip().rstrip("/")
    return out


def pick(names: str) -> tuple[dict[str, str], list[str]]:
    """(stores, unknown names) for 'all', or names / domains, comma-separated."""
    known = stores()
    wanted = [n.strip().lower() for n in re.split(r"[,\s]+", names or "") if n.strip()]
    if not wanted or "all" in wanted:
        return known, []
    out, unknown = {}, []
    for w in wanted:
        host = w.removeprefix("https://").removeprefix("http://").removeprefix("www.").split("/")[0]
        hit = next((n for n, u in known.items() if w == n or host and host in u), None)
        if hit:
            out[hit] = known[hit]
        else:
            unknown.append(w)
    return out, unknown


def _num(value) -> float | None:
    try:
        n = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return n or None


def _json(url: str) -> dict | list:
    with _lock:
        hit = _cache.get(url)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]  # type: ignore[return-value]
    page = fetch(url, {"Accept": "application/json"})
    if page.status == 429:
        raise RuntimeError("busy (the store is limiting requests from this server); try again in a few minutes")
    if page.status >= 400:
        raise RuntimeError(f"answered {page.status}")
    try:
        data = json.loads(page.body)
    except json.JSONDecodeError:
        raise RuntimeError("didn't answer with product data") from None
    with _lock:
        _cache[url] = (time.monotonic(), data)
        while len(_cache) > 512:
            _cache.popitem(last=False)
    return data


def _base(url: str) -> str:
    return url.removeprefix("web:").rstrip("/")


def search_store(name: str, url: str, query: str, limit: int = PER_STORE) -> list[StoreProduct]:
    base = _base(url)
    params = {"q": query, "resources[type]": "product", "resources[limit]": max(1, min(limit, 10)),
              "resources[options][unavailable_products]": "last"}
    data = _json(f"{base}/search/suggest.json?{urlencode(params)}")
    out = []
    for p in (data.get("resources", {}).get("results", {}).get("products") or []) if isinstance(data, dict) else []:
        link = p.get("url") or f"/products/{p.get('handle', '')}"
        out.append(StoreProduct(
            store=name, title=html.unescape(p.get("title", "")), url=base + link.split("?")[0],
            price=_num(p.get("price_min") or p.get("price")),
            mrp=_num(p.get("compare_at_price_max") or p.get("compare_at_price_min")),
            available=p.get("available"), kind=p.get("type", ""), vendor=p.get("vendor", ""),
            tags=[str(t) for t in (p.get("tags") or [])][:10],
        ))
    return out


def latest(name: str, url: str, limit: int = PER_STORE) -> list[StoreProduct]:
    """The store's newest products (Shopify lists them newest first)."""
    base = _base(url)
    data = _json(f"{base}/products.json?limit={max(1, min(limit, 250))}")
    out = []
    for p in data.get("products", []) if isinstance(data, dict) else []:
        variants = p.get("variants") or [{}]
        prices = [v for v in (_num(x.get("price")) for x in variants) if v]
        mrps = [v for v in (_num(x.get("compare_at_price")) for x in variants) if v]
        out.append(StoreProduct(
            store=name, title=html.unescape(p.get("title", "")), url=f"{base}/products/{p.get('handle', '')}",
            price=min(prices) if prices else None, mrp=max(mrps) if mrps else None,
            available=any(v.get("available") for v in variants) if "available" in variants[0] else None,
            kind=p.get("product_type", ""), vendor=p.get("vendor", ""),
            published=str(p.get("published_at") or p.get("created_at") or "")[:10],
            tags=[str(t) for t in (p.get("tags") or [])][:10] if isinstance(p.get("tags"), list) else [],
        ))
    return out


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def relevant(products: list[StoreProduct], query: str) -> list[StoreProduct]:
    """Store search is fuzzy ("neckband" also finds speakers): keep products sharing a word with the
    query in their title, type or tags; if that would leave nothing, keep them all."""
    want = _words(query)
    if not want:
        return products
    keep = [p for p in products if want & _words(" ".join([p.title, p.kind, *p.tags]))]
    return sorted(keep, key=lambda p: -score(p, want)) or products


def score(p: StoreProduct, want: set[str]) -> int:
    """How well a product matches: every query word in its type or title beats a passing mention."""
    kind, title = _words(p.kind), _words(p.title)
    return 3 * (want <= kind) + 2 * (want <= title) + len(want & kind) + len(want & (title | _words(" ".join(p.tags))))


def search_all(query: str, chosen: dict[str, str], per_store: int = PER_STORE,
               web=None) -> tuple[list[StoreProduct], list[str], list[str]]:
    """(products interleaved by store, problems, web-search lines for stores without an API).
    An empty query lists each store's newest products."""
    def one(item: tuple[str, str]):
        name, url = item
        if url.startswith("web:"):
            if web is None or not query:
                return name, [], None, None
            try:
                return name, [], None, (name, web(f"site:{urlsplit(_base(url)).hostname} {query}"))
            except Exception as e:  # noqa: BLE001
                return name, [], f"{name}: web search failed ({type(e).__name__})", None
        try:
            found = search_store(name, url, query, per_store) if query else latest(name, url, per_store)
            return name, (relevant(found, query) if query else found), None, None
        except Exception as e:  # noqa: BLE001 - one store down must not sink the rest
            return name, [], f"{name}: {str(e)[:80] or type(e).__name__}", None

    with ThreadPoolExecutor(PARALLEL) as pool:
        results = list(pool.map(one, chosen.items()))
    groups = [r[1] for r in results if r[1]]
    problems = [r[2] for r in results if r[2]]
    webbed = [r[3] for r in results if r[3]]
    merged = [p for row in _zip_longest(groups) for p in row]
    return merged, problems, webbed


def _zip_longest(groups: list[list]) -> list[list]:
    depth = max((len(g) for g in groups), default=0)
    return [[g[i] for g in groups if i < len(g)] for i in range(depth)]


def arrange(products: list[StoreProduct], sort: str = "relevance", min_price: float = 0, max_price: float = 0,
            in_stock: bool = False, query: str = "") -> list[StoreProduct]:
    out = [p for p in products
           if (not min_price or (p.price or 0) >= min_price) and (not max_price or (p.price and p.price <= max_price))
           and (not in_stock or p.available is not False)]
    if sort == "relevance" and query:
        want = _words(query)
        out.sort(key=lambda p: -score(p, want))  # stable: stores stay interleaved within a score
    elif sort == "price_low":
        out.sort(key=lambda p: p.price or float("inf"))
    elif sort == "price_high":
        out.sort(key=lambda p: -(p.price or 0))
    elif sort == "discount":
        out.sort(key=lambda p: -p.discount)
    elif sort == "new":
        out.sort(key=lambda p: p.published or "", reverse=True)
    return out


def is_store_page(url: str) -> bool:
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    return "/products/" in url and any(host and host == (urlsplit(_base(u)).hostname or "").removeprefix("www.")
                                       for u in stores().values() if not u.startswith("web:"))


def details(url: str) -> dict:
    """A store product page's facts from Shopify's own product JSON: price, MRP, variants, stock, text."""
    parts = urlsplit(url)
    handle = parts.path.split("/products/", 1)[1].split("/")[0]
    data = _json(f"{parts.scheme}://{parts.netloc}/products/{handle}.js")
    if not isinstance(data, dict):
        raise ValueError("no product data")
    variants = data.get("variants") or []
    price = min((v["price"] for v in variants if v.get("price")), default=data.get("price")) or 0
    mrp = max((v.get("compare_at_price") or 0 for v in variants), default=0)
    text = re.sub(r"<[^>]+>", " ", data.get("description") or "")
    facts = {
        "title": html.unescape(data.get("title", "")), "url": f"{parts.scheme}://{parts.netloc}/products/{handle}",
        "brand": data.get("vendor", ""), "type": data.get("type", ""),
        "price": f"₹{price / 100:,.0f}" if price else "",
        "mrp": f"₹{mrp / 100:,.0f}" if mrp and mrp > price else "",
        "availability": "in stock" if data.get("available") else "out of stock",
        "listed": str(data.get("published_at") or "")[:10],
        "variants": "; ".join(f"{v.get('title')}: ₹{(v.get('price') or 0) / 100:,.0f}"
                              + ("" if v.get("available") else " (out of stock)") for v in variants[:12]
                              if v.get("title") and v.get("title") != "Default Title"),
        "tags": ", ".join(map(str, (data.get("tags") or [])[:15])),
        "description": " ".join(html.unescape(text).split())[:2500],
    }
    return {k: v for k, v in facts.items() if v}


def render(products: list[StoreProduct], limit: int = 40) -> str:
    if not products:
        return "No matching products."
    return "\n".join(f"{i}. {p.line()}" for i, p in enumerate(products[:limit], 1))


def search_link(url: str, query: str) -> str:
    return f"{_base(url)}/search?q={quote_plus(query)}"
