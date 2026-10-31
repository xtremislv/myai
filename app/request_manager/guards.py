"""Pre-execution guards for the Request Manager:

  * NSFW detection -> block + report to HR and Admin
  * intent routing  -> orders / products / services / bookings / bills are
                       forwarded to the ACCOUNT department's inbox

Both are keyword heuristics for now (a model-based classifier can replace them
later behind the same interface).
"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.db.session import DepartmentTask, Report

# Clinical NSFW indicator tokens (kept minimal; routing/safety use only).
NSFW_TERMS = (
    "nsfw", "explicit sexual", "pornograph", "porn ", "sexually explicit",
    "nude photo", "child sexual", "csam", "bestiality",
)

# Intents that must be handled by the accounts department.
INTENT_KEYWORDS = {
    "order": ("order ", "place an order", "buy ", "purchase ", "procure "),
    "service": ("book ", "booking", "reserve ", "subscribe "),
    "bill": ("bill", "invoice", "pay the", "payment for"),
}
FORWARD_DEPARTMENT = "account"


def is_nsfw(message: str) -> tuple[bool, str]:
    text = message.lower()
    hit = next((t for t in NSFW_TERMS if t in text), None)
    return (hit is not None), (f"matched '{hit.strip()}'" if hit else "")


def report_nsfw(session: Session, subject_id: str, message: str) -> Report:
    row = Report(
        id=uuid.uuid4().hex,
        kind="nsfw",
        subject_id=subject_id,
        message=message,
        notified=["hr", "admin"],
        state="open",
    )
    session.add(row)
    session.commit()
    return row


def detect_forward_intent(message: str) -> str | None:
    """Return the task kind (order|service|bill) if the message should be
    forwarded to accounts, else None."""
    text = message.lower()
    for kind, words in INTENT_KEYWORDS.items():
        if any(w in text for w in words):
            return kind
    return None


def forward_to_account(session: Session, *, kind: str, requester_id: str, message: str, thread_id: str, mode: str) -> DepartmentTask:
    row = DepartmentTask(
        id=uuid.uuid4().hex,
        department=FORWARD_DEPARTMENT,
        requester_id=requester_id,
        kind=kind,
        message=message,
        thread_id=thread_id,
        mode=mode,
        state="queued",
    )
    session.add(row)
    session.commit()
    return row
