"""The ordering workflow fixes of 2026-11-04: shops that block the browser, Gemma's unclosed
thinking marker, disabled and covered buttons, destructive clicks, and stalls mid-order."""
from __future__ import annotations

import json

from app.browsing import extract, products
from app.browsing.browser import BLOCKED_NOTE, commit_reason
from app.llm.client import strip_thoughts
from app.orchestrator import nodes, specialists
from app.orchestrator import tools as tools_mod


# ---- block pages are recognised, so the agent stops and says so ----
def test_amazons_rush_hour_page_and_flipkarts_retry_page_are_blocks():
    amazon = ("503 - Service Unavailable Error", "Oops! It's rush hour and traffic is piling up on that page. "
              "Please try again in a short while.")
    flipkart = ("Online Shopping India | Buy Mobiles", "Wow, looks like all of India is here! Hang in there, the "
                "offers are worth the wait. Retry Now")
    assert extract.block_reason(*amazon)
    assert extract.block_reason(*flipkart)
    assert extract.block_reason("Noise Three Wireless Headphone", "₹1,799 Add to Cart " * 200) is None
    assert "don't click Retry" in BLOCKED_NOTE.format(site="amazon.in", why="x")


def test_a_blocked_shop_falls_back_to_one_that_works_and_gives_the_link(monkeypatch):
    monkeypatch.setattr(tools_mod, "_enforce", lambda *a: None)
    seen = []

    def search(query, sites, render=None):
        seen.append(sites)
        if sites == ("amazon",):
            return [], ["amazon: refused automated access"]
        return [products.Product("Noise Three", "https://www.reliancedigital.in/p/1", "reliancedigital.in", "₹1,799")], []

    monkeypatch.setattr(products, "search", search)
    out = tools_mod.product_search({"scratchpad": {}}, "noise three headphones", "amazon")
    assert seen == [("amazon",), ("reliancedigital",)]
    assert "Noise Three" in out and "instead" in out
    assert "https://www.amazon.in/s?k=noise+three+headphones" in out and "don't retry it" in out


# ---- Gemma's thinking marker never ends a run ----
def test_an_unclosed_thinking_marker_leaves_nothing_so_the_agent_retries():
    assert strip_thoughts("<|channel>thought") == ""
    assert strip_thoughts("<|channel>thought<|channel>thought\n<channel|>") == ""
    assert strip_thoughts("<|channel>thought\n<channel|>Hello") == "Hello"
    long = "Here is the comparison you asked for, with prices and ratings."
    assert strip_thoughts(f"<|channel>thought\n{long}") == long
    assert strip_thoughts("No markers here.") == "No markers here."


# ---- clicks ----
def test_emptying_the_cart_or_removing_items_needs_a_go_ahead():
    for label in ("Empty Cart", "Remove", "Clear bag", "Delete"):
        assert commit_reason({"text": label}), label
    for label in ("Add to Cart", "Go to cart", "Login to Proceed", "Set Pincode", "Apply"):
        assert commit_reason({"text": label}) is None, label


# ---- the agent doesn't loop on a call that already failed (except page actions) ----
def _call(i, name, args):
    return {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_a_failed_call_is_not_run_again_but_page_actions_may_be(monkeypatch):
    state = {"messages": [
        {"role": "user", "content": "order it"},
        {"role": "assistant", "content": "", "tool_calls": [_call(1, "list_saved_details", {"kind": "address"}),
                                                            _call(2, "browser_click", {"ref": 25})]},
        {"role": "tool", "tool_call_id": "c1", "name": "list_saved_details", "content": json.dumps("ERROR: nope")},
        {"role": "tool", "tool_call_id": "c2", "name": "browser_click", "content": json.dumps("ERROR: covered")},
    ]}
    failed = nodes._failed_this_turn(state)
    assert nodes._signature({"name": "list_saved_details", "args": {"kind": "address"}}) in failed
    assert nodes._signature({"name": "browser_click", "args": {"ref": 25}}) not in failed


# ---- a shopping specialist that stops halfway is sent back ----
def _own(*names):
    return [{"role": "assistant", "content": "", "tool_calls": [_call(i, n, {}) for i, n in enumerate(names)]}]


def test_an_order_that_stopped_halfway_is_unfinished():
    state = {"assignments": ["Order the Noise Three headphones from Reliance Digital, personal use"]}
    halfway = _own("product_search", "browser_open", "browser_click")
    assert specialists._unfinished_order("shopping", state, halfway, "I added it to your cart.")
    # Legitimate places to stop:
    assert not specialists._unfinished_order("shopping", state, _own("browser_open", "ask_person_to_browse"), "x")
    assert not specialists._unfinished_order("shopping", state, _own("browser_open", "browser_commit"), "x")
    assert not specialists._unfinished_order("shopping", state, halfway, "NEED INFO: delivery pincode")
    assert not specialists._unfinished_order("shopping", state, halfway, "BLOCKED: amazon.in")
    # Not an order, someone else, or nudged enough already:
    assert not specialists._unfinished_order("shopping", {"assignments": ["Compare earbuds under 1500"]}, halfway, "x")
    assert not specialists._unfinished_order("research", state, halfway, "x")
    assert not specialists._unfinished_order("shopping", {**state, "order_nudges": specialists.MAX_ORDER_NUDGES}, halfway, "x")
