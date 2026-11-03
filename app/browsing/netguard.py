"""Only public internet addresses, only http(s).

The gateway runs next to Postgres, Redis, MinIO, vLLM and Grafana on localhost, so a page
the model asks for must never resolve to them (SSRF). Every check resolves the host and
refuses unless every address is globally routable.

For httpx the check runs at connect time on the address actually dialled (GuardedBackend),
which also covers redirects and DNS rebinding. Chromium resolves names itself, so the browser
checks each request before it is sent (see browser.py); a rebinding attack between that check
and Chromium's own lookup is a known gap.
"""
from __future__ import annotations

import ipaddress
import socket
import time
from urllib.parse import urlsplit

import httpcore

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {80, 443, 8080, 8443}
# Names that only mean something inside a private network.
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".ts.net", ".intranet")
BLOCKED_HOSTS = {"localhost", "metadata", "metadata.google.internal"}

_CACHE_SECONDS = 60
_cache: dict[tuple[str, int], tuple[float, list[str]]] = {}


class Blocked(ValueError):
    """A URL or address the agents may not reach. The message is safe to show the model."""


def _public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


def resolve_public(host: str, port: int) -> list[str]:
    """The host's addresses, if every one of them is public. Raises Blocked otherwise."""
    host = host.strip("[]").lower().rstrip(".")
    if not host or host in BLOCKED_HOSTS or host.endswith(BLOCKED_SUFFIXES):
        raise Blocked(f"{host or 'that address'} is a private name")
    key = (host, port)
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise Blocked(f"{host} could not be found") from None
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        raise Blocked(f"{host} could not be found")
    if not all(_public(ip) for ip in ips):
        # Also what a DNS-poisoned name looks like from this server (e.g. 2001::1).
        raise Blocked(f"{host} does not resolve to a public internet address from this server")
    _cache[key] = (time.monotonic() + _CACHE_SECONDS, ips)
    return ips


def check_url(url: str) -> str:
    """Raise Blocked unless the URL is a public http(s) address. Returns it unchanged."""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise Blocked(f"only http and https links can be opened, not {parts.scheme or 'this'}")
    if parts.username or parts.password:
        raise Blocked("links with a username or password in them are not opened")
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError:
        raise Blocked("that link has an invalid port") from None
    if port not in ALLOWED_PORTS:
        raise Blocked(f"port {port} is not allowed")
    resolve_public(parts.hostname or "", port)
    return url.strip()


class GuardedBackend(httpcore.SyncBackend):
    """Dials only public addresses: the check runs on the very address connected to."""

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if port not in ALLOWED_PORTS:
            raise Blocked(f"port {port} is not allowed")
        # IPv4 first: many hosts (this one included) have no IPv6 route.
        ips = sorted(resolve_public(host, port), key=lambda ip: ":" in ip)
        error: Exception | None = None
        for ip in ips:
            try:
                return super().connect_tcp(ip, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                error = e
        raise error or httpcore.ConnectError(f"could not connect to {host}")
