"""Which sites can the agents reach from this server? Run it after any network or DNS change.

    .venv/bin/python -m scripts.check_connectivity

Each site is fetched through the agents' own path (network guard + HTTP client), so the result
is what an agent would get. Verdicts:
  ok         the page came back with text
  browser    plain HTTP was refused, but real Chromium (fetch_page's fallback) got the page,
             e.g. after an AWS WAF / Cloudflare check that passes on its own
  bot-block  refused even in Chromium (usually an edge rule against data-centre addresses,
             like Akamai's "Access Denied"): needs an official API or the person
  dns        the name resolves to a wrong or private address here (DNS poisoning)
  cut        the connection was reset, timed out or the network is unreachable (filtered)
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from app.browsing import extract
from app.browsing.fetch import fetch
from app.browsing.netguard import Blocked

SITES = {
    "search": ["https://www.bing.com/search?format=rss&q=test", "https://www.bing.com/news/search?format=rss&q=india",
               "https://duckduckgo.com/html/?q=test", "https://www.google.com/", "https://search.brave.com/",
               "https://api.tavily.com", "https://google.serper.dev"],
    "reference": ["https://en.wikipedia.org/wiki/India", "https://www.britannica.com/", "https://arxiv.org/",
                  "https://export.arxiv.org/api/query?search_query=all:battery&max_results=1",
                  "https://api.crossref.org/works?rows=1", "https://pubmed.ncbi.nlm.nih.gov/"],
    "news": ["https://www.reuters.com/", "https://www.bbc.com/news", "https://www.thehindu.com/",
             "https://timesofindia.indiatimes.com/", "https://www.livemint.com/", "https://economictimes.indiatimes.com/",
             "https://www.ndtv.com/", "https://www.business-standard.com/", "https://www.moneycontrol.com/"],
    "government/data": ["https://www.rbi.org.in/", "https://www.india.gov.in/", "https://data.gov.in/",
                        "https://www.worldbank.org/en/home", "https://api.worldbank.org/v2/country/IN?format=json"],
    "commerce/travel": ["https://www.amazon.in/", "https://www.flipkart.com/", "https://www.makemytrip.com/",
                        "https://www.irctc.co.in/", "https://www.booking.com/"],
    "tech": ["https://github.com/", "https://api.github.com/", "https://docs.python.org/3/", "https://pypi.org/",
             "https://huggingface.co/"],
    "apis": ["https://api.open-meteo.com/v1/forecast?latitude=19&longitude=72.8&current=temperature_2m"],
}


MIN_CONTENT = 300  # chars: less is an interstitial ("Continue shopping"), not the site


def _in_chromium(url: str) -> int:
    """Characters of real content Chromium gets, or 0 when it is refused too."""
    from app.browsing.browser import browser

    try:
        b = browser()
        title, text = extract.page_text(b.call(b.html_of, url))
    except Exception:  # noqa: BLE001 - a failed fallback is just a block
        return 0
    return 0 if extract.block_reason(title, text) or len(text) < MIN_CONTENT else len(text)


def probe(url: str) -> tuple[str, str, float]:
    started = time.monotonic()
    try:
        page = fetch(url)
    except Blocked as e:
        return "dns", str(e)[:70], time.monotonic() - started
    except Exception as e:  # noqa: BLE001
        return "cut", f"{type(e).__name__}: {str(e)[:60]}", time.monotonic() - started
    title, text = extract.page_text(page.body) if page.is_html else ("", page.body)
    if page.status in (401, 403, 429, 503) or (page.is_html and extract.block_reason(title, text)):
        if rendered := _in_chromium(url):
            return "browser", f"HTTP {page.status} plain, {rendered} chars in Chromium", time.monotonic() - started
        return "bot-block", f"HTTP {page.status}", time.monotonic() - started
    return "ok", f"HTTP {page.status}, {len(text)} chars", time.monotonic() - started


def main() -> None:
    urls = [u for group in SITES.values() for u in group]
    with ThreadPoolExecutor(12) as pool:
        results = dict(zip(urls, pool.map(probe, urls)))
    totals: dict[str, int] = {}
    for group, members in SITES.items():
        print(f"\n{group}")
        for url in members:
            verdict, detail, seconds = results[url]
            totals[verdict] = totals.get(verdict, 0) + 1
            print(f"  {verdict:10} {seconds:5.1f}s  {url[:60]:60}  {detail}")
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(totals.items())))


if __name__ == "__main__":
    main()
