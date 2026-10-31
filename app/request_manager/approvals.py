"""Approval store for critical requests + critical-action detection.

MVP gate: a request is 'critical' when its department requires approval and the
message hints at a critical operation (derived from the department's
`critical_resources` plus a small keyword set). This pre-execution heuristic
will be complemented by in-graph LangGraph interrupts once department agents
(Phase 3) land — the store and API stay the same.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy.orm import Session

from app.db.session import Approval, Department

# Keywords implying a side-effecting critical action.
CRITICAL_KEYWORDS = ("pay", "payment", "transfer", "transaction", "wire", "refund", "disburse", "remit")


def is_critical(session: Session, department: str, message: str) -> tuple[bool, str]:
    dept = session.get(Department, department)
    if dept is None or not dept.requires_approval:
        return False, "department does not require approval"
    text = message.lower()
    # tool:<name> critical resources contribute their bare tool name as a keyword.
    tool_words = [c.split(":", 1)[1] for c in (dept.critical_resources or []) if c.startswith("tool:")]
    words = list(CRITICAL_KEYWORDS) + [w.replace("_", " ") for w in tool_words] + tool_words
    hit = next((w for w in words if w and w in text), None)
    return (hit is not None), (f"matched '{hit}'" if hit else "no critical keyword")


def create(session: Session, *, department: str, subject_id: str, message: str, thread_id: str, mode: str) -> Approval:
    row = Approval(
        id=uuid.uuid4().hex,
        department=department,
        subject_id=subject_id,
        message=message,
        thread_id=thread_id,
        mode=mode,
        state="pending",
    )
    session.add(row)
    session.commit()
    return row


def list_pending(session: Session, department: str | None = None) -> list[Approval]:
    q = session.query(Approval).filter(Approval.state == "pending")
    if department:
        q = q.filter(Approval.department == department)
    return q.order_by(Approval.created_at).all()


def decide(session: Session, approval_id: str, *, approve: bool, decided_by: str, reason: str = "") -> Approval | None:
    row = session.get(Approval, approval_id)
    if row is None or row.state != "pending":
        return row
    row.state = "approved" if approve else "denied"
    row.decided_by = decided_by
    row.decided_at = dt.datetime.now(dt.timezone.utc)
    row.reason = reason
    session.commit()
    return row
