"""Approval store for critical tool calls.

The gate itself is in the graph: orchestrator.nodes.run_tool pauses on any tool
listed as critical in the department packs. This module persists what is being
decided, so an approver sees the exact tool and arguments.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy.orm import Session

from app.db.session import Approval


def create(
    session: Session,
    *,
    department: str,
    subject_id: str,
    message: str,
    thread_id: str,
    mode: str,
    tool: str,
    tool_args: dict,
    checkpoint_thread: str,
    task_id: str | None = None,
) -> Approval:
    row = Approval(
        id=uuid.uuid4().hex,
        department=department,
        subject_id=subject_id,
        message=message,
        thread_id=thread_id,
        mode=mode,
        tool=tool,
        tool_args=tool_args,
        checkpoint_thread=checkpoint_thread,
        task_id=task_id,
        state="pending",
    )
    session.add(row)
    session.commit()
    return row


def pending_for_thread(session: Session, checkpoint_thread: str) -> Approval | None:
    return (
        session.query(Approval)
        .filter(Approval.checkpoint_thread == checkpoint_thread, Approval.state == "pending")
        .first()
    )


def list_pending(session: Session, department: str | None = None) -> list[Approval]:
    q = session.query(Approval).filter(Approval.state == "pending")
    if department:
        q = q.filter(Approval.department == department)
    return q.order_by(Approval.created_at).all()


def decide(
    session: Session, approval_id: str, *, approve: bool, decided_by: str, reason: str = ""
) -> tuple[Approval | None, bool]:
    """Record a decision. Returns (row, won): `won` is True only for the one caller
    whose decision took effect, so a double approve cannot run a tool twice."""
    won = (
        session.query(Approval)
        .filter(Approval.id == approval_id, Approval.state == "pending")
        .update(
            {
                "state": "approved" if approve else "denied",
                "decided_by": decided_by,
                "decided_at": dt.datetime.now(dt.timezone.utc),
                "reason": reason,
            },
            synchronize_session=False,
        )
        == 1
    )
    session.commit()
    row = session.get(Approval, approval_id)
    if row is not None:
        session.refresh(row)
    return row, won
