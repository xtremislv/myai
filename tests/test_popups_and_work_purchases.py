"""2026-11-18 (owner): a popup over District's seat map was sent as a screenshot again and again, and a personal
movie booking on WhatsApp went to the Accounts inbox without asking."""
from __future__ import annotations

import pytest

from app.browsing import browser as browser_mod
from app.request_manager import guards

DISTRICT_LIKE = """
<html><body style="margin:0">
  <div id="page">
    <button>06:00 PM</button><button>06:30 PM</button><a href="/x">Seat A1</a><a href="/y">Seat A2</a>
    <button>Seat A3</button><button>Seat A4</button><button>Seat A5</button><button>Seat A6</button>
    <button>Seat A7</button><button>Seat A8</button><button>Proceed</button>
  </div>
  <div style="position:fixed;inset:0;backdrop-filter:blur(6px);background:rgba(255,255,255,.6);z-index:50"></div>
  <div style="position:fixed;top:25%;left:30%;width:40%;height:50%;background:#fff;z-index:60">
    <div class="sc-x1" style="position:absolute;top:8px;right:8px;width:24px;height:24px;cursor:pointer"><svg width="20" height="20"></svg></div>
    <h2>Introducing Best Seats</h2>
    <div style="cursor:pointer;margin:40px;padding:12px;background:#000;color:#fff">Continue booking</div>
  </div>
</body></html>
"""


@pytest.fixture
def page():
    playwright = pytest.importorskip("playwright.sync_api")
    try:
        with playwright.sync_playwright() as pw:
            chromium = pw.chromium.launch()
            tab = chromium.new_page(viewport={"width": 1280, "height": 900})
            yield tab
            chromium.close()
    except Exception as e:  # noqa: BLE001 - no browser on this machine
        pytest.skip(f"no Chromium here ({type(e).__name__})")


def test_a_popup_is_found_with_its_div_buttons_and_the_page_behind_it(page):
    page.set_content(DISTRICT_LIKE)
    elements = page.evaluate(browser_mod._MARK_JS, 150)
    popup = page.evaluate(browser_mod._OVERLAY_JS)
    assert popup and "Introducing Best Seats" in popup["text"]
    labels = {e["label"]: e["ref"] for e in popup["extra"]}
    assert set(labels) == {"× close", "Continue booking"}
    note = browser_mod.popup_note(popup, elements)
    assert note.startswith("POPUP:") and f"[{labels['× close']}] '× close'" in note
    page.click(f"[data-ai-ref='{labels['× close']}']")  # the numbered control is the real one
    page.set_content(DISTRICT_LIKE.split('<div style="position:fixed;inset:0')[0] + "</body></html>")
    page.evaluate(browser_mod._MARK_JS, 150)
    assert page.evaluate(browser_mod._OVERLAY_JS) is None  # no popup: nothing reported


def test_an_app_shell_is_not_a_popup(page):
    shell = ('<div style="position:fixed;inset:0;overflow:auto">' + "".join(f"<button>Item {n}</button>" for n in range(20))
             + "</div>")
    page.set_content(shell)
    page.evaluate(browser_mod._MARK_JS, 150)
    assert page.evaluate(browser_mod._OVERLAY_JS) is None


def test_popup_steps_are_safe_clicks_but_paying_is_not():
    for label in ("Continue booking", "Accept & continue", "I agree", "Select seats", "Got it", "OK"):
        assert browser_mod.commit_reason({"text": label}) is None, label
    for label in ("Book now and pay", "Pay ₹500", "Confirm booking", "Proceed to pay"):
        assert browser_mod.commit_reason({"text": label}) is not None, label


def test_show_person_holds_the_screenshot_while_a_popup_covers_the_page(monkeypatch):
    from app.orchestrator import tools
    from app.orchestrator.context import channel, shown_on_whatsapp

    class Fake:
        def has(self, key):
            return True

        def call(self, fn, *a):
            return fn(*a)

        def popup(self, key):
            return "POPUP: a popup/dialog covers the page ('Introducing Best Seats'). Its controls: [151] '× close'."

        def screen(self, key):
            return b"jpeg", "u", "t"

    sent = []
    monkeypatch.setattr(browser_mod, "browser", lambda: Fake())
    monkeypatch.setattr(tools, "_enforce", lambda *a: None)
    from app.whatsapp import bot

    monkeypatch.setattr(bot, "show", lambda uid, msg, pic, choices: sent.append(pic) or "Sent")
    tokens = (channel.set("whatsapp"), shown_on_whatsapp.set([]))
    try:
        state = {"subject_id": "u1", "scratchpad": {}}
        out = tools.show_person(state, "Pick your seats")
        assert out.startswith("NOT SENT: POPUP") and "[151] '× close'" in out and sent == []
        assert tools.show_person(state, "Solve this check", popup_ok=True).startswith("Shown")
        assert sent == [b"jpeg"]
    finally:
        shown_on_whatsapp.reset(tokens[1])
        channel.reset(tokens[0])


# ---- purchases go to Accounts only when the person said they're for work ----
@pytest.mark.parametrize("message,recent,work", [
    ("book 2 tickets for Ramayana in Noida tonight", [], False),
    ("continue booking", [{"role": "user", "content": "find showtimes in Noida"}], False),
    ("order the cheaper earbuds", [], False),
    ("Order 20 new office chairs for the Pune office.", [], True),
    ("Book a conference room at the hotel for the client visit", [], True),
    ("work", [{"role": "assistant", "content": "Is this personal or for work?"}], True),
    ("go ahead and book it", [{"role": "user", "content": "it's for the TechWiser shoot"}], True),
    ("the site doesn't work", [], False),
])
def test_says_work(message, recent, work):
    assert guards.says_work(message, recent) is work


def test_a_personal_booking_is_never_forwarded_to_accounts(monkeypatch):
    verdict = {"safety": "none", "route": "work", "department": "account", "action": "service", "steps": "one"}
    monkeypatch.setattr(guards, "triage_verdict", lambda *a, **k: verdict)
    depts = {"researcher": "research"}
    personal = guards.triage("continue booking the 10 PM show", depts, [])
    assert personal.route == "private" and personal.action is None
    work = guards.triage("book it, it's for the office team outing", depts, [])
    assert work.route == "work" and work.action == "service"
