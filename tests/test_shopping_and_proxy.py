"""Product research, shopping in the browser (cart and checkout free, ordering confirmed),
saved logins, the live view's access rules, and the outbound proxy's guard."""
from __future__ import annotations

import json
import os

import pytest

from app.browsing import logins, netguard, products
from app.browsing.browser import commit_reason, sensitive_reason
from app.orchestrator import tools
from app.orchestrator.context import browser_request

AMAZON = """
<div data-component-type="s-search-result"><h2><a href="/Boat-Earbuds/dp/B0X1/ref=sr_1"><span>boAt Airdopes 141 Earbuds</span></a></h2>
<span class="a-price"><span class="a-offscreen">₹1,099</span></span><span class="a-icon-alt">4.1 out of 5 stars</span>
<span class="a-size-base s-underline-text">(52,310)</span></div>
<div data-component-type="s-search-result"><span>Sponsored</span><h2><a href="/Noise-Buds/dp/B0X2"><span>Noise Buds VS102</span></a></h2>
<span class="a-price"><span class="a-offscreen">₹899</span></span></div>
"""

LD = json.dumps({"@context": "https://schema.org", "@type": "Product", "name": "Croma 1.5 Ton AC",
                 "url": "https://www.croma.com/p/123", "brand": {"@type": "Brand", "name": "Croma"},
                 "offers": {"@type": "Offer", "price": "32990", "availability": "https://schema.org/InStock"},
                 "aggregateRating": {"ratingValue": "4.2", "reviewCount": "310"}})

GENERIC = """
<div class="grid"><div class="card"><a href="/p/phone-x" title="Phone X 128 GB Midnight Black">Phone X 128 GB Midnight Black</a>
<div>₹24,999</div><div>4.4 ★</div></div>
<div class="card"><a href="/p/phone-y">Phone Y 256 GB Ocean Blue edition</a><span>Rs. 31,500</span></div></div>
"""


def test_amazon_results_are_read_with_price_rating_and_sponsored_flag():
    found = products.parse_results(AMAZON, "https://www.amazon.in/s?k=earbuds")
    assert [p.title for p in found] == ["boAt Airdopes 141 Earbuds", "Noise Buds VS102"]
    first = found[0]
    assert (first.price, first.rating, first.reviews) == ("₹1,099", "4.1", "52,310")
    assert first.url == "https://www.amazon.in/Boat-Earbuds/dp/B0X1"
    assert found[1].extra.get("sponsored")


def test_structured_data_is_trusted_first_and_generic_cards_fill_in():
    page = f'<script type="application/ld+json">{LD}</script><h1>Something else</h1>'
    facts = products.parse_page(page, "https://www.croma.com/p/123")
    assert facts["title"] == "Croma 1.5 Ton AC" and facts["price"] == "₹32,990"
    assert facts["rating"] == "4.2" and facts["availability"] == "InStock" and facts["brand"] == "Croma"
    cards = products.parse_results(GENERIC, "https://shop.example/search?q=phone")
    assert [(p.title[:7], p.price) for p in cards] == [("Phone X", "₹24,999"), ("Phone Y", "₹31,500")]


def test_search_reports_blocked_or_unknown_shops_instead_of_failing():
    def render(url):
        if "flipkart" in url:
            return "<title>Access Denied</title><body>You don't have permission</body>"
        return AMAZON
    found, problems = products.search("earbuds", ("amazon", "flipkart", "nykaa-unknown"), render=render)
    assert found and found[0].site == "amazon.in"
    assert any("flipkart: refused" in p for p in problems) and any("not a known shop" in p for p in problems)


# ---- what the agent may click and type ----
@pytest.mark.parametrize(("label", "submits", "blocked"), [
    ("Add to Cart", True, False),
    ("Proceed to checkout", True, False),
    ("Use this address", True, False),
    ("Buy Now", False, True),
    ("Place your order", True, True),
    ("Proceed to Pay", False, True),
    ("Continue", True, True),   # could be a payment step: needs the person
])
def test_cart_and_checkout_are_free_but_paying_needs_a_go_ahead(label, submits, blocked):
    assert (commit_reason({"text": label, "submits": submits}) is not None) is blocked


def test_otp_and_card_fields_are_never_typed_by_the_agent():
    assert sensitive_reason({"type": "text", "field": "enter otp"})
    assert sensitive_reason({"type": "tel", "field": "cardnumber cc-number"})
    assert sensitive_reason({"type": "text", "field": "search"}) is None


# ---- handing the browser to the person ----
def test_handover_is_recorded_for_the_chat_or_explained_when_nobody_watches(monkeypatch):
    monkeypatch.setattr(tools, "_enforce", lambda *a: None)
    state = {"subject_id": "alice", "subject_roles": [], "scratchpad": {}}
    assert "Nobody is watching" in tools.ask_person_to_browse(state, "Sign in to Amazon")
    holder: dict = {}
    token = browser_request.set(holder)
    try:
        assert "live browser" in tools.ask_person_to_browse(state, "Sign in to Amazon")
    finally:
        browser_request.reset(token)
    assert holder == {"scope": "private", "reason": "Sign in to Amazon"}


# ---- saved logins ----
def test_private_logins_are_saved_encrypted_with_the_persons_key(monkeypatch):
    from app.storage import objects

    blobs: dict[str, bytes] = {}
    monkeypatch.setattr(objects, "put_system_object", lambda k, d: blobs.__setitem__(k, d))
    monkeypatch.setattr(objects, "get_system_object", lambda k: blobs[k])
    monkeypatch.setattr(objects, "system_object_size", lambda k: len(blobs[k]) if k in blobs else None)
    key = os.urandom(32)
    state = {"cookies": [{"name": "session-id", "value": "SECRET-COOKIE", "domain": ".amazon.in"}], "origins": []}
    logins.save("alice", "private", state, key, force=True)
    assert b"SECRET-COOKIE" not in blobs["browserstate/alice"]
    assert logins.load("alice", "private", key) == state
    assert logins.load("alice", "private", os.urandom(32)) is None  # anyone else's key: nothing
    assert logins.load("alice", "private", None) is None


def test_the_admin_cannot_read_saved_work_logins():
    from app.departments.packs import ADMIN

    assert any(g.resource == "secret:browser/*" and g.effect == "deny" for g in ADMIN.grants)
    from app.api.routes_admin import PROTECTED_PREFIXES

    assert "browser/" in PROTECTED_PREFIXES


def test_live_view_only_opens_your_own_browsers():
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    from app.acl.models import Subject
    from app.api import routes_browser
    from app.auth.security import issue_token

    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=issue_token("alice", "alice", ["hr"], chat_key=os.urandom(32)))
    alice = Subject("alice", ("hr",))
    assert routes_browser._scope_key("hr", alice, creds)[0] == "alice:hr"
    assert routes_browser._scope_key("private", alice, creds)[0] == "alice:private"
    with pytest.raises(HTTPException):
        routes_browser._scope_key("account", alice, creds)


# ---- the outbound proxy keeps the guard ----
def test_with_a_proxy_poisoned_names_are_rechecked_through_encrypted_dns(monkeypatch):
    from app.config import get_settings

    netguard._cache.clear()
    monkeypatch.setattr(get_settings(), "outbound_proxy", "http://proxy.example:3128")
    monkeypatch.setattr(netguard.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("2001::1", 443))])
    monkeypatch.setattr(netguard, "_doh", lambda host: ["208.80.154.224"])
    assert netguard.resolve_public("en.wikipedia.org", 443) == ["208.80.154.224"]
    netguard._cache.clear()
    monkeypatch.setattr(netguard, "_doh", lambda host: ["10.0.0.5"])  # a name pointing inside a network
    with pytest.raises(netguard.Blocked):
        netguard.resolve_public("internal.example", 443)
    with pytest.raises(netguard.Blocked):
        netguard.check_url("http://127.0.0.1:8000/")
    netguard._cache.clear()


def test_without_a_proxy_poisoned_names_stay_blocked(monkeypatch):
    from app.config import get_settings

    netguard._cache.clear()
    monkeypatch.setattr(get_settings(), "outbound_proxy", "")
    monkeypatch.setattr(netguard.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("2001::1", 443))])
    monkeypatch.setattr(netguard, "_doh", lambda host: pytest.fail("no proxy, no lookup"))
    with pytest.raises(netguard.Blocked):
        netguard.resolve_public("en.wikipedia.org", 443)
    netguard._cache.clear()


def test_the_browser_and_http_client_use_the_proxy(monkeypatch):
    from app.browsing import browser as browser_mod
    from app.browsing import fetch
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "outbound_proxy", "http://u%40x:p%3Aw@proxy.example:3128")
    assert browser_mod._chromium_proxy() == {"server": "http://proxy.example:3128", "username": "u@x", "password": "p:w"}
    with fetch._client() as client:
        assert [t._pool._proxy_url.host for t in client._mounts.values()] == [b"proxy.example"]
    monkeypatch.setattr(get_settings(), "outbound_proxy", "")
    assert browser_mod._chromium_proxy() is None


def test_a_personal_purchase_is_never_forwarded_to_accounts(monkeypatch):
    from app.request_manager import guards

    monkeypatch.setattr(guards, "triage_verdict", lambda *a, **k: {
        "safety": "none", "route": "private", "department": None, "action": "order", "steps": "many"})
    got = guards.triage("Order me a phone cover on Amazon", {"researcher": "x"}, [])
    assert got.route == "private" and got.action is None
    monkeypatch.setattr(guards, "triage_verdict", lambda *a, **k: {
        "safety": "none", "route": "work", "department": None, "action": "order", "steps": "one"})
    assert guards.triage("Buy 5 office chairs for the team", {"researcher": "x"}, []).action == "order"
