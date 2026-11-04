"""An interactive headless browser for agents: open a page, read it, click, type, choose.

One isolated Chromium context per person (no shared cookies), closed after
`browser_idle_seconds`. Playwright runs on its own asyncio loop in one background thread;
graph nodes call the sync helpers below from any thread.

What the model sees of a page is a snapshot: the title, the readable text, and the visible
controls, each with a number (ref) it uses to act on them.

Safety rules, enforced here rather than trusted to the prompt:
  * Every request the page makes is checked by netguard (no localhost/private network),
    websockets too. Service workers and downloads are off.
  * Clicks that would commit something (book, pay, buy, send, submit a non-search form...)
    are refused by browser_click: they must go through browser_commit, which needs approval.
  * Passwords, card numbers, CVVs and one-time codes are never typed.
"""
from __future__ import annotations

import asyncio
import atexit
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from app.browsing import extract
from app.browsing.fetch import USER_AGENT
from app.browsing.netguard import Blocked, check_url
from app.config import get_settings

NAV_TIMEOUT_MS = 30_000
VIEWPORT = {"width": 1280, "height": 900}
# Shown above a page that is a site's block or "unavailable" answer to an automated browser. Don't
# retry or work around it: say so, and offer what still works.
BLOCKED_NOTE = (
    "BLOCKED: {site} refused this automated browser ({why}). This is not the real page: don't click "
    "Retry or open it again. Tell the person {site} blocks the assistant's browser, and offer instead: "
    "the same product on a shop that works (product_search with site='reliancedigital'), or the "
    "product link so they can order it themselves."
)


async def _click(handle, ref: int) -> None:
    """Click like a person would. Sticky headers often cover a button that was scrolled to the top
    edge ("... intercepts pointer events"): then centre it and try again, and as a last resort
    click the element itself."""
    try:
        await handle.click(timeout=6_000)
        return
    except Exception as first:  # noqa: BLE001
        covered = "intercepts pointer events" in str(first) or "Timeout" in str(first)
        if not covered:
            raise BrowserError(f"could not click [{ref}]: {_short(first)}") from None
    try:
        await handle.evaluate("e => e.scrollIntoView({block: 'center', inline: 'center'})")
        await handle.click(timeout=4_000)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        await handle.evaluate("e => e.click()")
    except Exception as e:  # noqa: BLE001
        raise BrowserError(f"could not click [{ref}]: {_short(e)}") from None


def _site_of(url: str) -> str:
    from urllib.parse import urlsplit

    host = urlsplit(url).hostname or "this site"
    return host.removeprefix("www.")
HUMAN_KEYS = {"Enter", "Tab", "Backspace", "Escape", "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Delete"}
CALL_TIMEOUT_S = 75
MAX_ELEMENTS = 60
PAGE_TEXT_CHARS = 2200

_MARK_JS = r"""
(maxCount) => {
  const SENSITIVE = /(password|passwd|card|cc-|cvv|cvc|security code|expiry|exp-|iban|account.?number|routing|otp|one-time|pin\b|ssn|aadhaar|pan\b|upi)/i;
  const sel = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],' +
              '[role=checkbox],[role=radio],[role=option],[role=menuitem],[role=combobox],[contenteditable=true]';
  document.querySelectorAll('[data-ai-ref]').forEach(e => e.removeAttribute('data-ai-ref'));
  const out = []; let i = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (el.type === 'hidden') continue;
    // Disabled buttons stay in the list, marked: "Add to cart" is often disabled until the page
    // gets something first (a delivery pincode, a size), and the agent must know it's there.
    const off = !!el.disabled || el.getAttribute('aria-disabled') === 'true';
    if (off && !['button', 'input'].includes(el.tagName.toLowerCase()) && el.getAttribute('role') !== 'button') continue;
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none') continue;
    i++; el.setAttribute('data-ai-ref', String(i));
    const tag = el.tagName.toLowerCase();
    const label = (el.getAttribute('aria-label') || el.innerText || el.getAttribute('placeholder') ||
                   el.title || el.getAttribute('name') || el.getAttribute('alt') ||
                   (tag === 'input' && ['submit','button'].includes(el.type) ? el.value : '') || '')
                   .trim().replace(/\s+/g, ' ').slice(0, 80);
    let value = '';
    const ident = [el.name, el.id, el.getAttribute('autocomplete'), el.getAttribute('placeholder'),
                   el.getAttribute('aria-label')].filter(Boolean).join(' ');
    const secret = el.type === 'password' || SENSITIVE.test(ident);
    if ((tag === 'input' || tag === 'textarea') && !secret) value = (el.value || '').slice(0, 40);
    if (secret && el.value) value = '(filled)';
    if (tag === 'select') value = (el.options[el.selectedIndex] || {}).text || '';
    const options = tag === 'select' ? Array.from(el.options).slice(0, 12).map(o => o.text.trim()) : [];
    out.push({ref: i, tag, kind: el.getAttribute('type') || el.getAttribute('role') || '', label, value, options,
              href: tag === 'a' ? (el.href || '').slice(0, 100) : '', off,
              inView: r.bottom > 0 && r.top < innerHeight});
    if (i >= maxCount) break;
  }
  return out;
}
"""

# What an element is, for the commit and sensitive-field checks.
_INFO_JS = r"""
(el) => {
  const form = el.form || el.closest('form');
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  const text = (el.getAttribute('aria-label') || el.innerText || el.value || el.title || '').trim().replace(/\s+/g, ' ').slice(0, 120);
  let searchForm = false;
  if (form) {
    const action = (form.getAttribute('action') || '').toLowerCase();
    searchForm = form.getAttribute('role') === 'search' || action.includes('search') ||
      !!form.querySelector('input[type=search]') ||
      Array.from(form.querySelectorAll('input[name]')).some(i => /^(q|query|search|keyword|keywords|s|k)$/i.test(i.name));
  }
  const submits = (tag === 'button' && (type === '' || type === 'submit') && !!form) ||
                  (tag === 'input' && (type === 'submit' || type === 'image'));
  const field = [el.getAttribute('name'), el.id, el.getAttribute('autocomplete'), el.getAttribute('placeholder'),
                 el.getAttribute('aria-label')].filter(Boolean).join(' ').toLowerCase();
  return {tag, type, text, submits, searchForm, field, role: el.getAttribute('role') || ''};
}
"""

# Steps that move toward a purchase without making it: the agent may click these; the final
# "Place your order" / "Pay" still needs browser_commit and a person's go-ahead.
SAFE_CLICK = re.compile(
    r"^\s*(add to (?:cart|bag|basket)|go to (?:cart|bag|basket)|view (?:cart|bag|basket)|cart|"
    r"proceed to checkout|(?:secure )?checkout|check out|deliver to this address|use this address)\b",
    re.IGNORECASE,
)
COMMIT_WORDS = re.compile(
    r"\b(book|booking|pay|payment|purchase|buy|place order|place your order|order now|confirm|"
    r"reserve|subscribe|send|submit|sign up|register|delete|transfer|donate|proceed to pay|"
    # Undoing what the person asked for (seen 2026-11-04: the agent clicked "Empty Cart" mid-checkout).
    r"empty (?:cart|bag|basket)|clear (?:cart|bag|basket)|remove|discard|"
    r"cancel (?:booking|order|subscription|reservation)|complete (?:booking|purchase|order))\b",
    re.IGNORECASE,
)
SENSITIVE_FIELD = re.compile(
    r"(password|passwd|card|cc-|cvv|cvc|security code|expiry|exp-|iban|account.?number|"
    r"routing|otp|one-time|pin\b|ssn|aadhaar|pan\b|upi)",
    re.IGNORECASE,
)
SEARCH_FIELD = re.compile(r"(search|query|\bq\b|keyword|find|destination|from|to|where|city)", re.IGNORECASE)


def commit_reason(info: dict[str, Any]) -> str | None:
    """Why clicking this element would commit something, or None if it looks safe to click."""
    text = info.get("text", "")
    if SAFE_CLICK.match(text) and not re.search(r"\b(pay|place|buy now|order now|purchase)\b", text, re.IGNORECASE):
        return None
    match = COMMIT_WORDS.search(text)
    if match:
        return f"its label says '{match.group(0)}'"
    if info.get("submits") and not info.get("searchForm"):
        return "it submits a form"
    return None


def sensitive_reason(info: dict[str, Any]) -> str | None:
    """Why this field must never be typed into by the agent, or None."""
    if info.get("type") == "password":
        return "it is a password field"
    if SENSITIVE_FIELD.search(info.get("field", "")):
        return "it asks for a password, card, bank or one-time code"
    return None


def enter_is_search(info: dict[str, Any]) -> bool:
    """Pressing Enter in this field only runs a search (safe), rather than submitting a form."""
    return (
        info.get("type") == "search"
        or info.get("role") in ("searchbox", "combobox")
        or bool(info.get("searchForm"))
        or (not info.get("submits") and bool(SEARCH_FIELD.search(info.get("field", ""))) and info.get("type") != "email")
    )


@dataclass
class _Session:
    context: Any
    page: Any
    last_used: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserError(RuntimeError):
    """Something went wrong on the page. The message is safe to show the model."""


class Browser:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._start_lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        self._pw = None
        self._chromium = None

    # ---- loop thread ----
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._start_lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._run_loop, name="browser", daemon=True).start()
                self._ready.wait(30)
                atexit.register(self.shutdown)
        return self._loop

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.create_task(self._reaper())
        self._ready.set()
        self._loop.run_forever()

    def call(self, coro_fn, *args) -> str:
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro_fn(*args), loop)
        try:
            return future.result(CALL_TIMEOUT_S)
        except TimeoutError:
            future.cancel()
            raise BrowserError("the page took too long to respond") from None

    async def _reaper(self) -> None:
        while True:
            await asyncio.sleep(30)
            idle = get_settings().browser_idle_seconds
            for key, s in list(self._sessions.items()):
                if time.monotonic() - s.last_used > idle and not s.lock.locked():
                    await self._close(key)

    def shutdown(self) -> None:
        """Close Chromium and Playwright's driver cleanly when the process exits."""
        if self._loop is None or not self._loop.is_running():
            return

        async def stop() -> None:
            for key in list(self._sessions):
                await self._close(key)
            if self._chromium is not None:
                await self._chromium.close()
            if self._pw is not None:
                await self._pw.stop()

        try:
            asyncio.run_coroutine_threadsafe(stop(), self._loop).result(10)
        except Exception:  # noqa: BLE001 - exiting anyway
            pass

    # ---- sessions ----
    async def _browser(self):
        if self._chromium is None or not self._chromium.is_connected():
            from playwright.async_api import async_playwright

            self._pw = self._pw or await async_playwright().start()
            self._chromium = await self._pw.chromium.launch(
                args=["--disable-dev-shm-usage", "--no-first-run", "--disable-background-networking"],
                proxy=_chromium_proxy(),
            )
        return self._chromium

    async def _session(self, key: str, create: bool = True, storage_state: dict | None = None) -> _Session:
        s = self._sessions.get(key)
        if s is not None:
            s.last_used = time.monotonic()
            return s
        if not create:
            raise BrowserError("no page is open; use browser_open first")
        if len(self._sessions) >= get_settings().browser_max_sessions:
            oldest = min(self._sessions, key=lambda k: self._sessions[k].last_used)
            await self._close(oldest)
        settings = get_settings()
        context = await (await self._browser()).new_context(
            user_agent=USER_AGENT, locale="en-IN", timezone_id=settings.timezone,
            viewport=VIEWPORT, accept_downloads=False,
            service_workers="block", java_script_enabled=True, storage_state=storage_state,
        )
        await context.route("**/*", _guard_request)
        await context.route_web_socket("**/*", _guard_websocket)
        page = await context.new_page()
        page.set_default_timeout(15_000)
        s = _Session(context, page)
        self._sessions[key] = s
        return s

    async def _close(self, key: str) -> None:
        s = self._sessions.pop(key, None)
        if s is not None:
            try:
                await s.context.close()
            except Exception:  # noqa: BLE001
                pass

    # ---- actions (run on the loop) ----
    async def _settle(self, page) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            await page.wait_for_load_state("networkidle", timeout=4_000)
        except Exception:  # noqa: BLE001 - busy pages never go idle; read what's there
            pass

    async def _snapshot(self, page, note: str = "") -> str:
        elements = await page.evaluate(_MARK_JS, 150)
        title, text = extract.page_text(await page.content())
        # After scrolling, show the text around where the page is now, not its top.
        ratio = await page.evaluate(
            "() => { const h = document.documentElement.scrollHeight - innerHeight;"
            " return h > 0 ? scrollY / h : 0 }"
        )
        text = text_from(text, ratio)
        visible = [e for e in elements if e["inView"]] + [e for e in elements if not e["inView"]]
        lines = []
        for e in visible[:MAX_ELEMENTS]:
            kind = e["tag"] + (f"[{e['kind']}]" if e["kind"] else "")
            line = f"[{e['ref']}] {kind} '{e['label']}'"
            if e["value"]:
                line += f" value='{e['value']}'"
            if e["options"]:
                line += " options=" + "/".join(e["options"])
            if e["href"] and not e["label"]:
                line += f" -> {e['href']}"
            if e.get("off"):
                line += " (disabled: the page needs something first, e.g. a delivery pincode or a size)"
            if not e["inView"]:
                line += " (below)"
            lines.append(line)
        more = f"\n…and {len(elements) - MAX_ELEMENTS} more controls (scroll to see them)" if len(elements) > MAX_ELEMENTS else ""
        blocked = extract.block_reason(title, text)
        if blocked:
            note = (f"{note}\n" if note else "") + BLOCKED_NOTE.format(site=_site_of(page.url), why=blocked)
        return (
            (f"{note}\n" if note else "")
            + f"PAGE: {title}\nURL: {page.url}\n\nTEXT:\n{extract.clip(text, PAGE_TEXT_CHARS)}\n\n"
            + "CONTROLS (use the [number] as ref):\n" + ("\n".join(lines) or "(none)") + more
        )

    async def _element(self, page, ref: int):
        handle = await page.query_selector(f'[data-ai-ref="{int(ref)}"]')
        if handle is None:
            raise BrowserError(f"there is no control [{ref}] on the page now; read the page again")
        return handle, await handle.evaluate(_INFO_JS)

    async def open(self, key: str, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        check_url(url)
        s = await self._session(key)
        async with s.lock:
            try:
                resp = await s.page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            except Exception as e:  # noqa: BLE001
                raise BrowserError(f"could not open {url}: {_short(e)}") from None
            await self._settle(s.page)
            status = f"(HTTP {resp.status})" if resp is not None and resp.status >= 400 else ""
            return await self._snapshot(s.page, status)

    async def read(self, key: str) -> str:
        s = await self._session(key, create=False)
        async with s.lock:
            return await self._snapshot(s.page)

    async def click(self, key: str, ref: int, commit: bool = False, expect: str = "") -> str:
        s = await self._session(key, create=False)
        async with s.lock:
            handle, info = await self._element(s.page, ref)
            if commit:
                if expect and expect.lower() not in info["text"].lower():
                    raise BrowserError(
                        f"control [{ref}] now says '{info['text']}', not '{expect}': the page changed, "
                        "so nothing was clicked. Read the page and ask again."
                    )
            else:
                said, wanted = info["text"].lower(), expect.strip().lower()
                if wanted and wanted not in said and said not in wanted:
                    raise BrowserError(
                        f"control [{ref}] says '{info['text']}', not '{expect}': read the page again for the right number"
                    )
                why = commit_reason(info)
                if why:
                    return (
                        f"NOT CLICKED: [{ref}] '{info['text']}' would commit something ({why}). "
                        "If the person asked for this, call browser_commit with this ref and its label; "
                        "a person must approve it."
                    )
            if await handle.evaluate("e => !!e.disabled || e.getAttribute('aria-disabled') === 'true'"):
                raise BrowserError(
                    f"[{ref}] '{info['text']}' is disabled: the page needs something first. Look for what it asks "
                    "for (often a delivery pincode: use the saved address's pincode, or ask the person) and do that."
                )
            await _click(handle, ref)
            await self._settle(s.page)
            note = f"Clicked [{ref}] '{info['text']}'." + (" (committed after approval)" if commit else "")
            return await self._snapshot(s.page, note)

    async def type(self, key: str, ref: int, text: str, press_enter: bool) -> str:
        s = await self._session(key, create=False)
        async with s.lock:
            handle, info = await self._element(s.page, ref)
            why = sensitive_reason(info)
            if why:
                return (
                    f"NOT TYPED: [{ref}] {why}. Agents never enter passwords, card or bank details; "
                    "the person has to fill that in themselves."
                )
            try:
                await handle.fill(text, timeout=10_000)
            except Exception:  # noqa: BLE001 - not a plain input (e.g. contenteditable)
                try:
                    await handle.click()
                    await s.page.keyboard.type(text, delay=20)
                except Exception as e:  # noqa: BLE001
                    raise BrowserError(f"could not type into [{ref}]: {_short(e)}") from None
            note = f"Typed into [{ref}]."
            if press_enter:
                if not enter_is_search(info):
                    note += " Did not press Enter: in this field it would submit a form. Click its button instead."
                else:
                    await handle.press("Enter")
                    await self._settle(s.page)
                    note += " Pressed Enter."
            else:
                await asyncio.sleep(0.8)  # let autocomplete suggestions appear
            return await self._snapshot(s.page, note)

    async def select(self, key: str, ref: int, option: str) -> str:
        s = await self._session(key, create=False)
        async with s.lock:
            handle, info = await self._element(s.page, ref)
            if info["tag"] != "select":
                return f"[{ref}] is not a dropdown list; click it to open it, then click the option."
            try:
                await handle.select_option(label=option, timeout=5_000)
            except Exception:  # noqa: BLE001
                try:
                    await handle.select_option(value=option, timeout=5_000)
                except Exception:  # noqa: BLE001
                    raise BrowserError(f"[{ref}] has no option '{option}'") from None
            await self._settle(s.page)
            return await self._snapshot(s.page, f"Chose '{option}' in [{ref}].")

    async def scroll(self, key: str, direction: str) -> str:
        s = await self._session(key, create=False)
        async with s.lock:
            dy = -800 if direction.lower().startswith("up") else 800
            await s.page.mouse.wheel(0, dy)
            await asyncio.sleep(0.8)
            return await self._snapshot(s.page, f"Scrolled {'up' if dy < 0 else 'down'}.")

    async def back(self, key: str) -> str:
        s = await self._session(key, create=False)
        async with s.lock:
            await s.page.go_back(timeout=NAV_TIMEOUT_MS)
            await self._settle(s.page)
            return await self._snapshot(s.page, "Went back.")

    # ---- saved details and secrets: typed by the server, never shown to the model ----
    async def url_of(self, key: str) -> str:
        s = await self._session(key, create=False)
        return s.page.url

    async def fill_value(self, key: str, ref: int, value: str, page_ok) -> str:
        """Type `value` into control [ref], if page_ok(current url) returns None (else its reason).
        Checked under the session lock, so the page can't change between the check and the typing."""
        s = await self._session(key, create=False)
        async with s.lock:
            why = page_ok(s.page.url)
            if why:
                raise BrowserError(f"not filled: {why}")
            handle, info = await self._element(s.page, ref)
            if info["tag"] not in ("input", "textarea") and info.get("role") not in ("textbox", "combobox"):
                raise BrowserError(f"[{ref}] is not a text field")
            try:
                await handle.fill(value, timeout=10_000)
            except Exception:  # noqa: BLE001
                try:
                    await handle.click()
                    await s.page.keyboard.type(value, delay=20)
                except Exception as e:  # noqa: BLE001
                    raise BrowserError(f"could not fill [{ref}]: {_short(e)}") from None
            await asyncio.sleep(0.5)
            return await self._snapshot(s.page, f"Filled [{ref}] '{info['text'] or info['field'][:40]}'.")

    # ---- saved logins and the person's live view (app/api/routes_browser.py) ----
    def has(self, key: str) -> bool:
        return key in self._sessions

    async def ensure(self, key: str, storage_state: dict | None) -> bool:
        """Open the session with a saved login (cookies) if it isn't open. True if it was created."""
        if key in self._sessions:
            return False
        await self._session(key, storage_state=storage_state)
        return True

    async def state_of(self, key: str) -> dict | None:
        """Cookies and local storage of an open session, to save its logins."""
        s = self._sessions.get(key)
        return None if s is None else await s.context.storage_state()

    async def screen(self, key: str) -> tuple[bytes, str, str]:
        """(JPEG of the visible page, url, title), for the person watching."""
        s = await self._session(key, create=False)
        async with s.lock:
            return await s.page.screenshot(type="jpeg", quality=60), s.page.url, await s.page.title()

    async def human(self, key: str, action: str, x: int = 0, y: int = 0, text: str = "", url: str = "") -> str:
        """An action by the person in the live view. Nothing they type is logged or returned."""
        s = await self._session(key, create=action == "open")
        async with s.lock:
            page = s.page
            if action == "open":
                if not url.startswith(("http://", "https://")):
                    url = "https://" + url
                check_url(url)
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                except Exception as e:  # noqa: BLE001
                    raise BrowserError(f"could not open {url}: {_short(e)}") from None
            elif action == "click":
                await page.mouse.click(max(0, min(x, VIEWPORT["width"])), max(0, min(y, VIEWPORT["height"])))
            elif action == "type":
                await page.keyboard.type(text[:500], delay=15)
            elif action == "key":
                if text not in HUMAN_KEYS:
                    raise BrowserError("that key isn't available")
                await page.keyboard.press(text)
            elif action == "scroll":
                await page.mouse.wheel(0, max(-3000, min(y, 3000)))
            elif action == "back":
                await page.go_back(timeout=NAV_TIMEOUT_MS)
            else:
                raise BrowserError("unknown action")
            await asyncio.sleep(0.6)
            return page.url

    async def html_of(self, url: str) -> str:
        """The rendered HTML of a public page, in a throwaway session (used by search)."""
        check_url(url)
        key = "__search__"
        s = await self._session(key)
        async with s.lock:
            await s.page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            try:
                await s.page.wait_for_load_state("load", timeout=5_000)
            except Exception:  # noqa: BLE001
                pass
            return await s.page.content()

    async def close(self, key: str) -> str:
        await self._close(key)
        return "Browser closed."


def _chromium_proxy() -> dict | None:
    """OUTBOUND_PROXY in Playwright's form (credentials separate from the server)."""
    from urllib.parse import urlsplit

    from app.browsing.netguard import proxy_url

    raw = proxy_url()
    if not raw:
        return None
    parts = urlsplit(raw)
    scheme = "socks5" if parts.scheme.startswith("socks5") else parts.scheme or "http"
    out: dict = {"server": f"{scheme}://{parts.hostname}:{parts.port or (1080 if scheme == 'socks5' else 3128)}"}
    if parts.username:
        from urllib.parse import unquote

        out["username"], out["password"] = unquote(parts.username), unquote(parts.password or "")
    return out


async def _guard_request(route) -> None:
    url = route.request.url
    if url.startswith(("data:", "blob:", "about:")):
        await route.continue_()
        return
    try:
        await asyncio.to_thread(check_url, url)
    except Blocked:
        await route.abort("blockedbyclient")
        return
    await route.continue_()


async def _guard_websocket(ws) -> None:
    try:
        await asyncio.to_thread(check_url, ws.url.replace("wss://", "https://", 1).replace("ws://", "http://", 1))
    except Blocked:
        await ws.close(code=1008, reason="blocked")
        return
    ws.connect_to_server()


def text_from(text: str, ratio: float) -> str:
    """The page text starting near `ratio` (0 = top, 1 = bottom) of the page."""
    if ratio <= 0.05 or len(text) <= PAGE_TEXT_CHARS:
        return text
    start = min(int(len(text) * ratio), len(text) - PAGE_TEXT_CHARS)
    start = text.rfind("\n", 0, start) + 1
    return f"[… {start} characters above]\n" + text[start:]


def _short(e: Exception) -> str:
    return str(e).split("\n", 1)[0][:200]


_browser = Browser()


def browser() -> Browser:
    return _browser
