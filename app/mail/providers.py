"""Mail providers people can connect with just their address and password.

The servers are chosen from the address: Gmail (and Google Workspace) by default, then the
provider named by the domain. Each entry says what that provider needs, so a failed login comes
back with the fix, not just "invalid credentials".

Measured 2026-11-03: Gmail accepts IMAP with an app password (needs 2-Step Verification on and
IMAP enabled), and answers a normal password with AUTHENTICATIONFAILED. Outlook/Microsoft 365
answer "Basic authentication is disabled": they need OAuth sign-in, which isn't built yet.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    name: str
    imap_host: str
    smtp_host: str
    smtp_port: int
    domains: tuple[str, ...]
    help: str
    password_ok: bool = True  # False: the provider refuses passwords for mail apps


GMAIL = Provider(
    "Gmail", "imap.gmail.com", "smtp.gmail.com", 465, ("gmail.com", "googlemail.com"),
    "Gmail needs an app password, not your normal one: turn on 2-Step Verification, then create one at "
    "https://myaccount.google.com/apppasswords (16 letters) and paste it here. Also check that IMAP is "
    "enabled in Gmail: Settings > See all settings > Forwarding and POP/IMAP.",
)
OUTLOOK = Provider(
    "Outlook", "outlook.office365.com", "smtp.office365.com", 587,
    ("outlook.com", "hotmail.com", "live.com", "msn.com", "outlook.in", "hotmail.co.in"),
    "Outlook and Microsoft 365 no longer accept passwords from mail apps (Microsoft requires its own "
    "sign-in, OAuth, which this app doesn't support yet). Use a Gmail address, or forward your Outlook "
    "mail to one.",
    password_ok=False,
)
YAHOO = Provider(
    "Yahoo", "imap.mail.yahoo.com", "smtp.mail.yahoo.com", 465, ("yahoo.com", "yahoo.co.in", "ymail.com"),
    "Yahoo needs an app password: Account security > Generate app password.",
)
ZOHO = Provider(
    "Zoho", "imap.zoho.in", "smtp.zoho.in", 465, ("zoho.com", "zoho.in", "zohomail.in"),
    "Zoho needs IMAP access turned on (Settings > Mail accounts > IMAP) and, with two-factor sign-in, an app password.",
)
ICLOUD = Provider(
    "iCloud", "imap.mail.me.com", "smtp.mail.me.com", 587, ("icloud.com", "me.com", "mac.com"),
    "iCloud needs an app-specific password from appleid.apple.com.",
)
PROVIDERS = (GMAIL, OUTLOOK, YAHOO, ZOHO, ICLOUD)


def candidates(address: str) -> list[Provider]:
    """Providers to try, best guess first: the one the domain names; otherwise Gmail (Google
    Workspace runs company domains too), then Outlook."""
    domain = address.rsplit("@", 1)[-1].strip().lower()
    named = [p for p in PROVIDERS if domain in p.domains]
    return named or [GMAIL, OUTLOOK]


def explain(provider: Provider, server_said: str) -> str:
    said = server_said.strip().strip("b'\"")[:160]
    return f"{provider.name} refused the sign-in ({said}). {provider.help}"
