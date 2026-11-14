"""Gadget brand stores (app/browsing/stores.py), gadget_search and the store rows of web_research."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from app.browsing import stores
from app.browsing.fetch import Fetched
from app.orchestrator import tools as tools_mod

STATE = {"messages": [], "subject_id": "u1", "subject_roles": ["researcher"], "scratchpad": {}}


def _suggest(*items):
    return {"resources": {"results": {"products": [
        {"title": t, "url": f"/products/{h}?_pos=1", "price_min": price, "compare_at_price_max": mrp,
         "available": ok, "type": kind, "vendor": "V", "tags": []} for t, h, price, mrp, ok, kind in items]}}}


@pytest.fixture
def shop(monkeypatch):
    calls = []
    monkeypatch.setattr(stores, "DEFAULT_STORES", {"alpha": "https://alpha.in", "beta": "https://www.beta.com",
                                                   "web1": "web:https://www.web1.com"})
    stores._cache.clear()

    def fake_fetch(url, headers=None):
        parts = urlsplit(url)
        calls.append(url)
        host, path = parts.hostname, parts.path
        if path == "/search/suggest.json":
            q = parse_qs(parts.query)["q"][0]
            body = (_suggest(("Aero 10 Power Bank", "aero", "699.00", "1999.00", True, "Power Bank"),
                             ("Mini Lighter for power bank users", "lighter", "299.00", "", True, "Lighter"),
                             ("Desk Lamp", "lamp", "999.00", "", True, "Lamp"))
                    if host == "alpha.in" else
                    _suggest(("Juice 20000 Power Bank", "juice", "1499.00", "1499.00", False, "Power Bank")))
            assert q
        elif path == "/products.json":
            body = {"products": [{"title": f"New {host}", "handle": "new", "product_type": "Charger",
                                  "published_at": "2026-11-1" + ("4" if host == "alpha.in" else "2") + "T00:00:00",
                                  "variants": [{"price": "499.00", "compare_at_price": "999.00", "available": True}]}]}
        elif path.endswith(".js"):
            body = {"title": "Aero 10", "vendor": "Alpha", "type": "Power Bank", "price": 69900, "available": True,
                    "variants": [{"title": "Black", "price": 69900, "compare_at_price": 199900, "available": True}],
                    "description": "<p>10000mAh, 20W</p>", "tags": ["powerbank"], "published_at": "2026-11-01"}
        else:
            raise AssertionError(url)
        return Fetched(url, 200, "application/json", json.dumps(body))

    monkeypatch.setattr(stores, "fetch", fake_fetch)
    return calls


def test_all_stores_are_searched_and_weak_matches_dropped(shop):
    found, problems, webbed = stores.search_all("power bank", stores.stores(), web=lambda q: f"results for {q}")
    titles = [p.title for p in found]
    assert "Desk Lamp" not in titles and "Aero 10 Power Bank" in titles and "Juice 20000 Power Bank" in titles
    assert problems == [] and webbed == [("web1", "results for site:www.web1.com power bank")]
    ranked = stores.arrange(found, "relevance", query="power bank")
    assert ranked[-1].title.startswith("Mini Lighter")  # only mentions the words: last
    aero = next(p for p in found if p.title.startswith("Aero"))
    assert aero.url == "https://alpha.in/products/aero" and aero.discount == 65


def test_filters_and_sorts(shop):
    found, _, _ = stores.search_all("power bank", stores.stores())
    cheap = stores.arrange(found, "price_low", max_price=1000, in_stock=True)
    assert [p.price for p in cheap] == [299.0, 699.0]
    assert stores.arrange(found, "discount")[0].title.startswith("Aero")


def test_no_query_lists_the_newest_products(shop):
    found, _, webbed = stores.search_all("", stores.stores(), 5)
    newest = stores.arrange(found, "new")
    assert [p.store for p in newest] == ["alpha", "beta"] and newest[0].published == "2026-11-14"
    assert webbed == []  # a web-only store has no "newest" list


def test_answers_are_cached(shop):
    stores.search_all("power bank", stores.stores())
    stores.search_all("power bank", stores.stores())
    assert len([c for c in shop if "suggest" in c]) == 2  # one per Shopify store, not four


def test_a_throttled_store_is_reported_not_retried(shop, monkeypatch):
    real = stores.fetch

    def throttled(url, headers=None):
        if "beta" in url:
            return Fetched(url, 429, "text/plain", "local_rate_limited")
        return real(url, headers)

    monkeypatch.setattr(stores, "fetch", throttled)
    found, problems, _ = stores.search_all("power bank", stores.stores())
    assert found and problems and problems[0].startswith("beta: busy")


def test_store_names_and_domains_pick_stores(shop):
    picked, unknown = stores.pick("alpha, www.beta.com/collections/x, nowhere")
    assert list(picked) == ["alpha", "beta"] and unknown == ["nowhere"]


def test_product_details_come_from_the_store_json(shop):
    assert stores.is_store_page("https://alpha.in/products/aero") and not stores.is_store_page("https://amazon.in/products/x")
    facts = stores.details("https://alpha.in/products/aero?variant=1")
    assert facts["price"] == "₹699" and facts["mrp"] == "₹1,999" and "10000mAh" in facts["description"]


def test_gadget_search_tool_lists_and_saves(shop, monkeypatch):
    saved = {}
    monkeypatch.setattr(tools_mod, "_enforce", lambda *a, **k: None)
    monkeypatch.setattr(tools_mod, "_search_api", lambda: None)
    from app.browsing import search

    monkeypatch.setattr(search, "web", lambda q, api=None: [])
    monkeypatch.setattr(tools_mod, "_save", lambda state, path, data, **k: saved.setdefault(path, data) and f"Created '{path}'.")
    out = tools_mod.TOOLS["gadget_search"](STATE, query="power bank", sort="price_low", max_price=1000, save=True)
    assert out.startswith("<<UNTRUSTED CONTENT from gadget brand stores")
    assert "₹699 (MRP ₹1,999, 65% off)" in out and "https://alpha.in/products/aero" in out and "Created" in out
    assert len(saved) == 1 and next(iter(saved)).endswith(".xlsx")
    bad = tools_mod.TOOLS["gadget_search"](STATE, query="x", stores="nowhere")
    assert bad.startswith("ERROR: no such store")


def test_web_research_gets_store_products_as_rows(shop):
    rows = tools_mod._store_rows("power bank", "", ["Name", "Brand", "Price", "Link", "Why it fits"])
    aero = next(r for r in rows if r["Name"].startswith("Aero"))
    assert aero == {"_source": "https://alpha.in/products/aero", "Name": "Aero 10 Power Bank", "Brand": "alpha",
                    "Price": "₹699 (MRP ₹1,999)", "Link": "https://alpha.in/products/aero"}


def test_newest_listings_can_be_limited_to_recent_days(shop, monkeypatch):
    import datetime as dt

    class Today(dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 14)

    monkeypatch.setattr("datetime.date", Today)
    monkeypatch.setattr(tools_mod, "_enforce", lambda *a, **k: None)
    monkeypatch.setattr(tools_mod, "_search_api", lambda: None)
    out = tools_mod.TOOLS["gadget_search"](STATE, days=1)
    assert "New alpha.in" in out and "New www.beta.com" not in out  # listed on the 14th vs the 12th


def test_links_in_answers_are_checked_against_tool_results():
    from app.orchestrator.links import fix_links, sources

    real = "https://alpha.in/products/nano-power-bank-10000-mah-22-5w-power-delivery-usb-c"
    msgs = [{"role": "user", "content": "q"}, {"role": "tool", "content": f"1. Nano\n   {real}\n2. https://alpha.in/products/imperial-power-bank"}]
    out = fix_links("[Nano](https://alpha.in/products/nano-power-bank-1) and https://alpha.in/ and https://other.com/x-y-z",
                    sources(msgs))
    assert out == f"[Nano]({real}) and https://alpha.in/ and https://other.com/x-y-z"
