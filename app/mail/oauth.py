"""Sign in to Gmail and Outlook with OAuth instead of a password.

Why: Outlook/Microsoft 365 refuse passwords for mail apps, and some Google accounts can't make
app passwords (Workspace policy, Advanced Protection). With OAuth the person signs in on Google's
or Microsoft's own page; we keep only a refresh token, encrypted in the vault next to the mailbox
settings, and log in to IMAP/SMTP with XOAUTH2.

The company registers one app with each provider and stores its client in the vault (admin,
Credentials page):

  oauth/google     {"client_id": "...", "client_secret": "...", "redirect_uri": "(optional)"}
  oauth/microsoft  {"client_id": "...", "tenant": "common"}

Google: a "Desktop app" OAuth client. Its sign-in ends on http://127.0.0.1 (the person's own
machine, where nothing answers), so the person pastes that address back into the app: this
works although the quick-tunnel address changes. With a stable domain, set redirect_uri to
https://<domain>/v1/me/mailbox/oauth/google/callback (a "Web application" client) and the
callback finishes on its own. Gmail's scope (https://mail.google.com/) is "restricted": an app
left in Testing mode gets refresh tokens that expire after 7 days, so publish it (unverified
apps work for up to 100 people, behind a warning screen).

Microsoft: device-code sign-in (the person opens microsoft.com/devicelogin and types a short
code), so no redirect address is needed. The app registration needs "Allow public client flows"
and the delegated permissions IMAP.AccessAsUser.All, SMTP.Send and offline_access.

Token traffic goes direct, like the mail itself (not through OUTBOUND_PROXY).
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass

import httpx

from app.db.session import SessionLocal
from app.mail import providers
from app.vault.aesgcm_vault import AESGCMVault

TIMEOUT = 20
FLOW_SECONDS = 15 * 60  # a started sign-in must finish within this


@dataclass(frozen=True)
class OAuthProvider:
    name: str  # "google" | "microsoft"
    label: str
    token_url: str
    scopes: str
    mail: providers.Provider
    auth_url: str = ""
    device_url: str = ""


GOOGLE = OAuthProvider(
    "google", "Google", "https://oauth2.googleapis.com/token",
    "https://mail.google.com/ openid email", providers.GMAIL,
    auth_url="https://accounts.google.com/o/oauth2/v2/auth",
)
MICROSOFT = OAuthProvider(
    "microsoft", "Microsoft", "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
    "offline_access https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/SMTP.Send "
    "openid email profile",
    providers.OUTLOOK,
    device_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode",
)
OAUTH = {p.name: p for p in (GOOGLE, MICROSOFT)}
LOOPBACK = "http://127.0.0.1:8765/"


class OAuthError(Exception):
    """Something the person (or the admin) can act on; the text is safe to show them."""


# ---- the company's app registration ----
def client(provider: OAuthProvider) -> dict | None:
    with SessionLocal() as session:
        raw = AESGCMVault(session).get(f"oauth/{provider.name}")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if data.get("client_id") else None


def configured() -> dict[str, bool]:
    return {name: client(p) is not None for name, p in OAUTH.items()}


def _tenant(cfg: dict) -> str:
    tenant = str(cfg.get("tenant") or "common")
    return tenant if tenant.replace("-", "").replace(".", "").isalnum() else "common"


def _url(template: str, cfg: dict) -> str:
    return template.format(tenant=_tenant(cfg))


def _post(url: str, data: dict) -> dict:
    """POST a form to a token endpoint. Raises OAuthError with the provider's own words."""
    try:
        response = httpx.post(url, data=data, timeout=TIMEOUT, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise OAuthError(f"couldn't reach the sign-in server ({type(e).__name__}); try again") from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400 and body.get("error") not in ("authorization_pending", "slow_down"):
        said = body.get("error_description") or body.get("error") or f"HTTP {response.status_code}"
        raise OAuthError(f"the sign-in server said: {str(said).splitlines()[0][:200]}")
    return body


def _claims(id_token: str) -> dict:
    """The id token's claims. It came straight from the token endpoint over TLS, so it isn't
    re-verified; only the address is read from it."""
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


# ---- sign-ins in progress (one per person; in memory: a gateway restart just means start again) ----
_flows: dict[str, dict] = {}
_lock = threading.Lock()


def _keep(owner: str, flow: dict) -> None:
    now = time.time()
    with _lock:
        for key in [k for k, f in _flows.items() if f["expires"] < now]:
            del _flows[key]
        _flows[owner] = {**flow, "expires": now + FLOW_SECONDS}


def _flow(owner: str, provider: str) -> dict:
    with _lock:
        flow = _flows.get(owner)
    if not flow or flow["provider"] != provider or flow["expires"] < time.time():
        raise OAuthError("that sign-in has expired; start it again")
    return flow


def _drop(owner: str) -> None:
    with _lock:
        _flows.pop(owner, None)


def owner_of_state(state: str) -> str | None:
    with _lock:
        return next((o for o, f in _flows.items() if f.get("state") and secrets.compare_digest(f["state"], state)), None)


def _need(provider: OAuthProvider) -> dict:
    cfg = client(provider)
    if cfg is None:
        raise OAuthError(
            f"Sign-in with {provider.label} isn't set up on this server yet: an admin must add the "
            f"vault secret 'oauth/{provider.name}' (see docs/SERVER_SETUP.md, 'Mail sign-in')."
        )
    return cfg


# ---- Google: authorization code + PKCE ----
def google_start(owner: str, login_hint: str = "") -> dict:
    cfg = _need(GOOGLE)
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(24)
    redirect = cfg.get("redirect_uri") or LOOPBACK
    params = {
        "client_id": cfg["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": GOOGLE.scopes, "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(),
        "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    _keep(owner, {"provider": "google", "verifier": verifier, "state": state, "redirect": redirect})
    return {"url": f"{GOOGLE.auth_url}?{urllib.parse.urlencode(params)}", "paste": redirect == LOOPBACK}


def code_from(pasted: str) -> tuple[str, str]:
    """(code, state) from the address the browser ended on (or a bare code)."""
    pasted = pasted.strip()
    if "code=" not in pasted:
        if pasted and " " not in pasted and "://" not in pasted:  # a bare code, e.g. 4/0Ab...
            return pasted, ""
        raise OAuthError("paste the whole address from the browser's address bar (it contains 'code=')")
    query = urllib.parse.urlsplit(pasted).query or pasted.split("?", 1)[-1]
    values = urllib.parse.parse_qs(query)
    if values.get("error"):
        raise OAuthError(f"Google said: {values['error'][0]}")
    return values.get("code", [""])[0], values.get("state", [""])[0]


def google_finish(owner: str, code: str, state: str) -> dict:
    """Exchange the code; returns the mailbox secret (dict) to verify and save."""
    flow = _flow(owner, "google")
    if state and not secrets.compare_digest(state, flow["state"]):
        raise OAuthError("that address belongs to a different sign-in; start again")
    if not code:
        raise OAuthError("no sign-in code found in what you pasted")
    cfg = _need(GOOGLE)
    tokens = _post(GOOGLE.token_url, {
        "client_id": cfg["client_id"], "client_secret": cfg.get("client_secret", ""), "code": code,
        "code_verifier": flow["verifier"], "redirect_uri": flow["redirect"], "grant_type": "authorization_code",
    })
    _drop(owner)
    return _secret(GOOGLE, tokens)


# ---- Microsoft: device code ----
def microsoft_start(owner: str) -> dict:
    cfg = _need(MICROSOFT)
    body = _post(_url(MICROSOFT.device_url, cfg), {"client_id": cfg["client_id"], "scope": MICROSOFT.scopes})
    if not body.get("device_code"):
        raise OAuthError("Microsoft didn't start the sign-in; try again")
    _keep(owner, {"provider": "microsoft", "device_code": body["device_code"],
                  "interval": int(body.get("interval", 5)), "next": 0.0})
    return {
        "user_code": body.get("user_code", ""),
        "verification_uri": body.get("verification_uri", "https://microsoft.com/devicelogin"),
        "interval": int(body.get("interval", 5)),
        "expires_in": int(body.get("expires_in", 900)),
    }


def microsoft_poll(owner: str) -> dict | None:
    """The mailbox secret once the person has signed in; None while they haven't yet."""
    flow = _flow(owner, "microsoft")
    if time.time() < flow.get("next", 0):
        return None
    cfg = _need(MICROSOFT)
    body = _post(_url(MICROSOFT.token_url, cfg), {
        "client_id": cfg["client_id"], "device_code": flow["device_code"],
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    })
    if body.get("error") in ("authorization_pending", "slow_down"):
        _keep(owner, {**flow, "next": time.time() + flow["interval"] - 1})
        return None
    _drop(owner)
    return _secret(MICROSOFT, body)


# ---- tokens ----
def _secret(provider: OAuthProvider, tokens: dict) -> dict:
    if not tokens.get("refresh_token") or not tokens.get("access_token"):
        raise OAuthError(f"{provider.label} didn't grant offline mail access; start again and allow every permission")
    claims = _claims(tokens.get("id_token", ""))
    address = (claims.get("email") or claims.get("preferred_username") or "").strip()
    if "@" not in address:
        raise OAuthError(f"{provider.label} didn't say which address signed in; start again")
    mail = provider.mail
    return {
        "auth": "oauth", "provider": provider.name, "imap_host": mail.imap_host, "imap_port": 993,
        "smtp_host": mail.smtp_host, "smtp_port": mail.smtp_port, "username": address, "from": address,
        "password": "", "refresh_token": tokens["refresh_token"], "access_token": tokens["access_token"],
        "expires_at": int(time.time()) + int(tokens.get("expires_in", 3600)),
    }


def access_token(secret_name: str, data: dict) -> str:
    """A current access token for the mailbox stored under `secret_name` (whose settings are
    `data`), refreshing it, and saving the new tokens, when it is about to expire."""
    if data.get("access_token") and int(data.get("expires_at", 0)) > time.time() + 120:
        return data["access_token"]
    if secret_name:  # another call may have refreshed it already
        with SessionLocal() as session:
            raw = AESGCMVault(session).get(secret_name)
        if raw:
            data = json.loads(raw)
            if data.get("access_token") and int(data.get("expires_at", 0)) > time.time() + 120:
                return data["access_token"]
    provider = OAUTH.get(data.get("provider", ""))
    if provider is None:
        raise OAuthError("this mailbox's sign-in is incomplete; connect it again")
    cfg = _need(provider)
    form = {"client_id": cfg["client_id"], "refresh_token": data["refresh_token"], "grant_type": "refresh_token"}
    if provider is GOOGLE:
        form["client_secret"] = cfg.get("client_secret", "")
    else:
        form["scope"] = provider.scopes
    try:
        tokens = _post(_url(provider.token_url, cfg), form)
    except OAuthError as e:
        raise OAuthError(f"{e}. The mailbox needs to be connected again under My account.") from None
    data = {
        **data, "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token") or data["refresh_token"],  # Microsoft rotates it
        "expires_at": int(time.time()) + int(tokens.get("expires_in", 3600)),
    }
    if secret_name:
        with SessionLocal() as session:
            AESGCMVault(session).put(secret_name, json.dumps(data))
    return data["access_token"]


def xoauth2(username: str, token: str) -> str:
    return f"user={username}\x01auth=Bearer {token}\x01\x01"
