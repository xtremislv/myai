"""Every email matching a search, as spreadsheet rows: who, when, what, and (optionally) a one-line
summary, the action needed and any deadline for each. Used by the mail_to_sheet tool, in the
background when there are many."""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.mail import connector, summary

MAX_EXPORT = 5000        # emails in one sheet
MAX_SUMMARIZED = 2000    # emails summarized by the model in one sheet (~10 min on this GPU)
BODY_BYTES = 64 * 1024   # of each email, for its summary (text comes before attachments)
BODY_CHARS = 1500        # of that text, handed to the model
BATCH = 8                # emails per model call
PARALLEL = 6             # model calls at once

HEADER_COLUMNS = ["Date", "From", "From email", "Subject", "Unread"]
SUMMARY_COLUMNS = ["Date", "From", "From email", "Subject", "Summary", "Action needed", "Deadline",
                   "Urgent", "Type", "Attachments", "Unread"]


def when(raw: str) -> str:
    """An email Date header as "YYYY-MM-DD HH:MM" in the company's time zone ("" if unreadable)."""
    try:
        stamp = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return ""
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(ZoneInfo(get_settings().timezone)).strftime("%Y-%m-%d %H:%M")


def name_of(sender: str, address: str) -> str:
    name = sender.replace(f"<{address}>", "").strip().strip('"') if address else sender
    return name or address


def header_rows(mails: list[dict]) -> list[dict]:
    return [{
        "Date": when(m["date"]), "From": name_of(m["from"], m["from_email"]), "From email": m["from_email"],
        "Subject": m["subject"], "Unread": "yes" if m["unread"] else "",
    } for m in mails]


def summary_rows(mails: list[dict], progress: Callable[[str], None] | None = None, chat=None) -> list[dict]:
    for m in mails:
        m["body"] = m["body"][:BODY_CHARS]
    items = summary.analyse(mails, chat=chat, progress=progress, batch_size=BATCH, parallel=PARALLEL)
    by_id = {i["id"]: i for i in items}
    rows = []
    for m in mails:
        i = by_id.get(m["id"], {})
        rows.append({
            "Date": when(m["date"]), "From": name_of(m["from"], m["from_email"]), "From email": m["from_email"],
            "Subject": m["subject"], "Summary": i.get("gist", ""), "Action needed": i.get("action", ""),
            "Deadline": i.get("deadline", ""), "Urgent": "yes" if i.get("urgent") else "",
            "Type": i.get("kind", ""), "Attachments": ", ".join(m.get("attachments") or []),
            "Unread": "yes" if m["unread"] else "",
        })
    return rows


def build(cfg: connector.MailConfig, box: str, query: str, count: int, summarize: bool,
          progress: Callable[[str], None] = lambda _t: None, chat=None) -> tuple[int, list[str], list[dict]]:
    """(total matching, columns, rows) for the newest `count` matching emails."""
    def fetched(done: int, of: int) -> None:
        progress(f"Fetched {done} of {of} emails")

    total, mails = connector.search_all(cfg, box, query, count, BODY_BYTES if summarize else 0, fetched)
    if not summarize:
        return total, HEADER_COLUMNS, header_rows(mails)
    return total, SUMMARY_COLUMNS, summary_rows(mails, progress, chat)
