"""Meta's WhatsApp Cloud API: settings, webhook signatures, and the messages the bot sends.

Settings are the vault secret `whatsapp/cloud` (Credentials page), a JSON object:
  token          a System User access token that never expires (whatsapp_business_messaging + _management)
  phone_id       the Phone number ID from WhatsApp > API Setup (an id, not the number)
  app_secret     App settings > Basic: Meta signs every webhook call with it
  verify_token   any random string, also typed into the webhook settings in Meta
  display_number the bot's number with country code, digits only (shown to people when they link)
  template       optional: an approved Utility template with one body parameter, for notifications outside
                 WhatsApp's 24-hour window ("You have a new notification on TW-ai: {{1}}")
  template_lang  its language code (default "en")
  api_version    Graph API version (default DEFAULT_VERSION)

Traffic to Meta goes direct (not OUTBOUND_PROXY, which is for the agents' web traffic only).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.vault.aesgcm_vault import AESGCMVault

logger = logging.getLogger(__name__)

SECRET = "whatsapp/cloud"
DEFAULT_VERSION = "v23.0"
GRAPH = "https://graph.facebook.com"
TEXT_LIMIT = 4096        # WhatsApp's limit for one text message
BUTTON_BODY_LIMIT = 1024
BUTTON_TITLE_LIMIT = 20
TEMPLATE_PARAM_LIMIT = 200
OUTSIDE_WINDOW = 131047  # "Re-engagement message": more than 24 hours since the person last wrote


class CloudError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Config:
    token: str
    phone_id: str
    app_secret: str
    verify_token: str
    display_number: str = ""
    template: str = ""
    template_lang: str = "en"
    api_version: str = DEFAULT_VERSION


def config(session: Session) -> Config | None:
    """None until the admin has added a complete `whatsapp/cloud` secret."""
    raw = AESGCMVault(session).get(SECRET)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        cfg = Config(
            token=str(data["token"]).strip(), phone_id=str(data["phone_id"]).strip(),
            app_secret=str(data["app_secret"]).strip(), verify_token=str(data["verify_token"]).strip(),
            display_number=re.sub(r"\D", "", str(data.get("display_number", ""))),
            template=str(data.get("template", "")).strip(), template_lang=str(data.get("template_lang") or "en"),
            api_version=str(data.get("api_version") or DEFAULT_VERSION),
        )
    except (ValueError, KeyError, TypeError):
        logger.warning("the whatsapp/cloud secret is not valid JSON with token, phone_id, app_secret, verify_token")
        return None
    return cfg if all((cfg.token, cfg.phone_id, cfg.app_secret, cfg.verify_token)) else None


def signature_ok(app_secret: str, body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256: "sha256=" + HMAC-SHA256 of the raw body with the app secret."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


# Tests replace this with an httpx.MockTransport.
transport: httpx.BaseTransport | None = None


def _post(cfg: Config, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{GRAPH}/{cfg.api_version}/{cfg.phone_id}/messages"
    with httpx.Client(timeout=20, transport=transport) as client:
        resp = client.post(url, json={"messaging_product": "whatsapp", **payload},
                           headers={"Authorization": f"Bearer {cfg.token}"})
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error", {})
        except ValueError:
            err = {}
        # The code and Meta's short message only: never the payload (it holds the person's text).
        raise CloudError(f"WhatsApp API {resp.status_code}: {err.get('message', 'error')}", err.get("code"))
    return resp.json()


def send_text(cfg: Config, to: str, body: str) -> dict[str, Any]:
    body = body if len(body) <= TEXT_LIMIT else body[:TEXT_LIMIT - 1] + "…"
    return _post(cfg, {"to": to, "type": "text", "text": {"body": body, "preview_url": True}})


def send_buttons(cfg: Config, to: str, body: str, buttons: list[tuple[str, str]]) -> dict[str, Any]:
    """Up to three reply buttons: (id, title)."""
    body = body if len(body) <= BUTTON_BODY_LIMIT else body[:BUTTON_BODY_LIMIT - 1] + "…"
    return _post(cfg, {"to": to, "type": "interactive", "interactive": {
        "type": "button", "body": {"text": body},
        "action": {"buttons": [{"type": "reply", "reply": {"id": i[:256], "title": t[:BUTTON_TITLE_LIMIT]}}
                               for i, t in buttons[:3]]}}})


CAPTION_LIMIT = 1024


def upload_media(cfg: Config, data: bytes, mime: str, filename: str) -> str:
    """Upload a file the bot sends (a screenshot) to Meta; returns its media id."""
    url = f"{GRAPH}/{cfg.api_version}/{cfg.phone_id}/media"
    with httpx.Client(timeout=60, transport=transport) as client:
        resp = client.post(url, headers={"Authorization": f"Bearer {cfg.token}"},
                           data={"messaging_product": "whatsapp", "type": mime},
                           files={"file": (filename, data, mime)})
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error", {})
        except ValueError:
            err = {}
        raise CloudError(f"WhatsApp media upload {resp.status_code}: {err.get('message', 'error')}", err.get("code"))
    media_id = str(resp.json().get("id") or "")
    if not media_id:
        raise CloudError("WhatsApp media upload returned no id")
    return media_id


def send_image(cfg: Config, to: str, data: bytes, caption: str = "", mime: str = "image/jpeg") -> dict[str, Any]:
    caption = caption if len(caption) <= CAPTION_LIMIT else caption[:CAPTION_LIMIT - 1] + "…"
    media_id = upload_media(cfg, data, mime, "screenshot.jpg" if mime == "image/jpeg" else "picture")
    return _post(cfg, {"to": to, "type": "image", "image": {"id": media_id, **({"caption": caption} if caption else {})}})


def template_param(text: str) -> str:
    """Template parameters may not hold new lines, tabs or more than four spaces in a row."""
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= TEMPLATE_PARAM_LIMIT else flat[:TEMPLATE_PARAM_LIMIT - 1] + "…"


def send_template(cfg: Config, to: str, params: list[str]) -> dict[str, Any]:
    if not cfg.template:
        raise CloudError("no notification template is configured (template in the whatsapp/cloud secret)")
    return _post(cfg, {"to": to, "type": "template", "template": {
        "name": cfg.template, "language": {"code": cfg.template_lang},
        "components": [{"type": "body", "parameters": [{"type": "text", "text": template_param(p)} for p in params]}]}})


def mark_read(cfg: Config, message_id: str, typing: bool = True) -> None:
    """Blue ticks, and "typing..." until the reply is sent (up to ~25 s, Meta's limit). An API version without
    typing indicators gets the plain read receipt."""
    try:
        _post(cfg, {"status": "read", "message_id": message_id,
                    **({"typing_indicator": {"type": "text"}} if typing else {})})
    except (CloudError, httpx.HTTPError):
        if typing:
            mark_read(cfg, message_id, typing=False)


class MediaTooLarge(CloudError):
    pass


def download_media(cfg: Config, media_id: str, max_bytes: int) -> tuple[bytes, str]:
    """(content, mime type) of a voice note or document someone sent. Meta sends only an id: look up its short-lived
    URL, then fetch it with the same token. Refuses files over max_bytes before and while downloading."""
    auth = {"Authorization": f"Bearer {cfg.token}"}
    with httpx.Client(timeout=60, transport=transport, follow_redirects=True) as client:
        meta = client.get(f"{GRAPH}/{cfg.api_version}/{media_id}", headers=auth)
        if meta.status_code >= 400:
            raise CloudError(f"WhatsApp media lookup {meta.status_code}")
        info = meta.json()
        if int(info.get("file_size") or 0) > max_bytes:
            raise MediaTooLarge("file too large")
        url = str(info.get("url") or "")
        if not url.startswith("https://"):
            raise CloudError("WhatsApp media has no download address")
        chunks, size = [], 0
        with client.stream("GET", url, headers=auth) as resp:
            if resp.status_code >= 400:
                raise CloudError(f"WhatsApp media download {resp.status_code}")
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise MediaTooLarge("file too large")
                chunks.append(chunk)
    return b"".join(chunks), str(info.get("mime_type") or "application/octet-stream")


def incoming(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The messages in a webhook call (status updates and other fields are skipped), each with "from", "id",
    "type" and the type's own object, plus "phone_id" (which of our numbers it was sent to)."""
    out = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            phone_id = (value.get("metadata") or {}).get("phone_number_id", "")
            for message in value.get("messages") or []:
                if isinstance(message, dict) and message.get("from") and message.get("id"):
                    out.append({**message, "phone_id": phone_id})
    return out
