"""Fetch one public page (or PDF/JSON/text) through the network guard."""
from __future__ import annotations

import io
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from app.browsing.netguard import Blocked, GuardedBackend, check_url

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36"
)
MAX_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


@dataclass(frozen=True)
class Fetched:
    url: str           # after redirects
    status: int
    content_type: str
    body: str          # decoded text (PDFs: their extracted text)

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type or "xml" in self.content_type and "<html" in self.body[:2000].lower()


def _client() -> httpx.Client:
    transport = httpx.HTTPTransport(retries=1)
    transport._pool._network_backend = GuardedBackend()  # httpx has no public hook for this
    return httpx.Client(
        transport=transport, timeout=TIMEOUT, follow_redirects=False, trust_env=False,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"},
    )


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages[:30])


def post_json(url: str, payload: dict, headers: dict[str, str]) -> dict:
    """POST JSON to a public API through the guard (used for search APIs). No redirects."""
    check_url(url)
    with _client() as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()


def fetch(url: str) -> Fetched:
    """GET a URL, following up to MAX_REDIRECTS redirects, each re-checked. Raises Blocked
    for addresses agents may not reach and httpx.HTTPError for network failures."""
    current = check_url(url)
    with _client() as client:
        for _ in range(MAX_REDIRECTS + 1):
            with client.stream("GET", current) as resp:
                if resp.is_redirect and resp.headers.get("location"):
                    current = check_url(urljoin(current, resp.headers["location"]))
                    continue
                data = bytearray()
                for chunk in resp.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        break
                ctype = resp.headers.get("content-type", "").lower()
                if "pdf" in ctype or current.lower().endswith(".pdf"):
                    body = _pdf_text(bytes(data))
                else:
                    body = bytes(data).decode(resp.encoding or "utf-8", errors="replace")
                return Fetched(str(resp.url), resp.status_code, ctype, body)
    raise Blocked("too many redirects")
