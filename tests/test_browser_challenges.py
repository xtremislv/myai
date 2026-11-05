"""Self-passing bot checks (AWS WAF's empty 202 page, Cloudflare's "just a moment") are waited
out in the real browser; hard refusals (Akamai "Access Denied") and CAPTCHAs are not."""
from __future__ import annotations

import asyncio

from app.browsing import browser, extract


def test_which_pages_are_worth_waiting_for():
    assert extract.passing_check("", "")  # AWS WAF: empty page, no title
    assert extract.passing_check("Just a moment...", "Checking your browser before accessing")
    assert not extract.passing_check("Access Denied", "You don't have permission to access this server.")
    assert not extract.passing_check("Robot check", "Enter the characters you see below (captcha)")
    assert not extract.passing_check("Hotels in Goa", "Beachfront 419 Swimming pool 2254 " * 30)


class FakePage:
    def __init__(self, pages):
        self.pages = list(pages)
        self.reads = 0

    async def content(self):
        self.reads += 1
        return self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]


REAL = "<html><head><title>Hotels in Goa</title></head><body>" + "Beachfront hotel " * 40 + "</body></html>"


def test_the_browser_waits_until_the_check_has_passed(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(browser.asyncio, "sleep", no_sleep)
    page = FakePage(["<html></html>", "<html></html>", REAL])
    asyncio.run(browser._pass_challenge(page))
    assert page.reads == 3


def test_a_hard_refusal_is_not_waited_for(monkeypatch):
    page = FakePage(["<html><title>Access Denied</title><body>You don't have permission</body></html>"])
    asyncio.run(browser._pass_challenge(page))
    assert page.reads == 1


def test_the_wait_has_a_limit():
    page = FakePage(["<html></html>"])
    asyncio.run(browser._pass_challenge(page, wait_s=0.05))
    assert page.reads >= 1
