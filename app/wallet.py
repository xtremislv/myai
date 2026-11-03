"""A person's saved details: website logins, cards, UPI IDs and addresses.

Encrypted with the owner's chat key, so only they can open them (the admin can't, and nor can
work requests, which run without the key). Agents see only masked labels ("Visa •••• 4242") and
ask the server to fill a detail into a page field; the value never reaches the model or the chat.

Where a detail may be typed, enforced here and in the browser:
  * a login only on its own site (amazon.in -> www.amazon.in, not amazon-offers.example);
  * a card or UPI ID only on a known shop or payment gateway, or the card's own extra sites;
  * an address on any page (it is what shops ask for at checkout).
CVVs are never stored (card rules forbid it); the person types those, like OTPs, into a secure
prompt for each payment.
"""
from __future__ import annotations

import json
import re
import uuid
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.db.session import WalletItem
from app.vault import user_keys

FIELDS = {
    "login": ("label", "site", "username", "password"),
    "card": ("label", "name", "number", "expiry", "sites"),
    "upi": ("label", "upi_id", "sites"),
    "address": ("label", "name", "phone", "line1", "line2", "city", "state", "pincode"),
}
SECRET_FIELDS = {"password", "number", "upi_id"}
# Shops and payment gateways where a card or UPI ID may be typed (plus any `sites` on the card).
PAYMENT_SITES = (
    "amazon.in", "amazon.com", "flipkart.com", "myntra.com", "ajio.com", "nykaa.com", "tatacliq.com",
    "croma.com", "reliancedigital.in", "jiomart.com", "bigbasket.com", "swiggy.com", "zomato.com",
    "makemytrip.com", "irctc.co.in", "goibibo.com", "bookmyshow.com",
    "razorpay.com", "payu.in", "payumoney.com", "ccavenue.com", "billdesk.com", "paytm.com", "paytm.in",
    "phonepe.com", "juspay.in", "cashfree.com", "stripe.com", "paypal.com", "amazonpay.in", "pinelabs.com",
)
_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "ac", "edu", "res", "gen", "firm", "ind"}


class WalletError(ValueError):
    """Something the person (or the agent) can fix; safe to show."""


def site_of(url_or_host: str) -> str:
    """The registrable domain: https://www.amazon.in/x -> amazon.in, pay.irctc.co.in -> irctc.co.in."""
    host = (urlsplit(url_or_host).hostname if "//" in url_or_host else url_or_host) or ""
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _luhn(number: str) -> bool:
    digits = [int(d) for d in number][::-1]
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2) for i, d in enumerate(digits))
    return total % 10 == 0


def _clean(kind: str, data: dict) -> dict:
    if kind not in FIELDS:
        raise WalletError(f"kind must be one of {', '.join(FIELDS)}")
    if any(k.lower() in ("cvv", "cvc", "cvv2", "pin", "otp") for k in data):
        raise WalletError("CVVs, PINs and OTPs are never saved: you'll be asked for them at each payment")
    out = {k: str(data.get(k) or "").strip() for k in FIELDS[kind]}
    if kind == "login":
        if not out["site"] or not out["username"] or not out["password"]:
            raise WalletError("a login needs the site, username and password")
        out["site"] = site_of(out["site"] if "." in out["site"] else out["site"] + ".com")
    if kind == "card":
        number = re.sub(r"[\s-]", "", out["number"])
        if not re.fullmatch(r"\d{12,19}", number) or not _luhn(number):
            raise WalletError("that card number isn't valid")
        out["number"] = number
        if not re.fullmatch(r"(0[1-9]|1[0-2])/\d{2}", out["expiry"]):
            raise WalletError("expiry must look like 08/28")
    if kind == "upi" and not re.fullmatch(r"[\w.\-]{2,}@[\w]{2,}", out["upi_id"]):
        raise WalletError("a UPI ID looks like name@bank")
    if kind == "address" and not (out["line1"] and out["pincode"]):
        raise WalletError("an address needs at least the first line and the PIN code")
    out["sites"] = ",".join(site_of(s.strip()) for s in out.get("sites", "").split(",") if s.strip()) if "sites" in out else ""
    return {k: v for k, v in out.items() if v or k == "label"}


def _ctx(owner: str, item_id: str) -> str:
    return f"wallet:{owner}:{item_id}"


def add(session: Session, key: bytes, owner: str, kind: str, data: dict) -> str:
    clean = _clean(kind, data)
    row = WalletItem(id=uuid.uuid4().hex[:12], owner_id=owner, kind=kind)
    row.encrypted = user_keys.encrypt_text(key, json.dumps(clean), _ctx(owner, row.id))
    session.add(row)
    session.commit()
    return row.id


def _open(key: bytes, row: WalletItem) -> dict:
    return json.loads(user_keys.decrypt_text(key, row.encrypted, _ctx(row.owner_id, row.id)))


def get(session: Session, key: bytes, owner: str, item_id: str) -> tuple[str, dict]:
    row = session.get(WalletItem, item_id)
    if row is None or row.owner_id != owner:
        raise WalletError(f"there is no saved detail '{item_id}' (list_saved_details shows them)")
    try:
        return row.kind, _open(key, row)
    except Exception:  # noqa: BLE001 - saved under a key nobody can open any more
        raise WalletError(f"saved detail '{item_id}' can't be opened (it was saved before a password reset)") from None


def _mask(value: str, keep: int = 4) -> str:
    return "•••• " + value[-keep:] if len(value) > keep else "••••"


def describe(kind: str, data: dict) -> str:
    """What the agent and the list show: never a whole secret."""
    label = data.get("label") or ""
    if kind == "login":
        user = data["username"]
        shown = user[:2] + "•••" + (user[user.find("@"):] if "@" in user else "")
        return f"Login for {data['site']} ({shown})" + (f": {label}" if label else "")
    if kind == "card":
        brand = {"4": "Visa", "5": "Mastercard", "6": "RuPay", "3": "Amex"}.get(data["number"][0], "Card")
        return f"{label or brand} {_mask(data['number'])}, expires {data['expiry']}" + (f" ({data['name']})" if data.get("name") else "")
    if kind == "upi":
        handle, bank = data["upi_id"].split("@", 1)
        return f"UPI {label + ': ' if label else ''}{handle[:2]}•••@{bank}"
    city = ", ".join(x for x in (data.get("city"), data.get("pincode")) if x)
    return f"Address{': ' + label if label else ''} ({data.get('name', '')}, {city})"


def listing(session: Session, key: bytes, owner: str, kind: str = "") -> list[dict]:
    query = session.query(WalletItem).filter(WalletItem.owner_id == owner)
    if kind:
        query = query.filter(WalletItem.kind == kind)
    out = []
    for row in query.order_by(WalletItem.created_at).all():
        try:
            data = _open(key, row)
        except Exception:  # noqa: BLE001
            continue
        out.append({"id": row.id, "kind": row.kind, "summary": describe(row.kind, data),
                    "site": data.get("site", ""), "label": data.get("label", "")})
    return out


def delete(session: Session, owner: str, item_id: str) -> bool:
    row = session.get(WalletItem, item_id)
    if row is None or row.owner_id != owner:
        return False
    session.delete(row)
    session.commit()
    return True


def erase_owner(session: Session, owner: str) -> int:
    n = session.query(WalletItem).filter(WalletItem.owner_id == owner).delete()
    session.commit()
    return n


def allowed_on(kind: str, data: dict, page_url: str) -> str | None:
    """None if this detail may be typed on this page, else why not."""
    site = site_of(page_url)
    if kind == "login":
        return None if site == data["site"] else f"this login is for {data['site']}, but the page is on {site}"
    if kind in ("card", "upi"):
        extra = [s for s in data.get("sites", "").split(",") if s]
        if site in PAYMENT_SITES or site in extra:
            return None
        return (f"{site} isn't a known shop or payment page, so payment details aren't typed there. If it is "
                "genuine, the person can type them in the live browser, or add the site to the card.")
    return None


def field_value(kind: str, data: dict, field: str) -> str:
    """One field to type. Card expiry also comes as month, year (2 or 4 digits)."""
    if kind == "card" and field in ("expiry_month", "expiry_year", "expiry_year4"):
        month, year = data["expiry"].split("/")
        return {"expiry_month": month, "expiry_year": year, "expiry_year4": "20" + year}[field]
    if field not in data or field in ("label", "sites"):
        allowed = [f for f in FIELDS[kind] if f not in ("label", "sites")] + (
            ["expiry_month", "expiry_year", "expiry_year4"] if kind == "card" else [])
        raise WalletError(f"a saved {kind} has no field '{field}'; use one of: {', '.join(allowed)}")
    return data[field]
