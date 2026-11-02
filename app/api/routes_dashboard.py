"""Counts for the web dashboard. Each caller only sees numbers for what their roles
can already reach. Nothing here touches private chats: they have no audit rows."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.acl.models import Subject
from app.api.deps import current_subject
from app.db.session import Approval, AuditLog, Department, DepartmentTask, Report, User, get_session

router = APIRouter(prefix="/v1/dashboard", tags=["dashboard"])

WINDOW = dt.timedelta(hours=24)


def _activity(session: Session, since: dt.datetime, subject_id: str | None) -> list[dict]:
    query = session.query(AuditLog.resource, AuditLog.decision, func.count()).filter(
        AuditLog.action == "request", AuditLog.ts >= since
    )
    if subject_id is not None:
        query = query.filter(AuditLog.subject == subject_id)
    rows = query.group_by(AuditLog.resource, AuditLog.decision).all()
    return [
        {"department": resource.removeprefix("dept:"), "outcome": decision, "count": count}
        for resource, decision, count in rows
    ]


@router.get("")
def overview(
    subject: Subject = Depends(current_subject),
    session: Session = Depends(get_session),
) -> dict:
    roles = set(subject.roles)
    is_admin = "admin" in roles
    since = dt.datetime.now(dt.timezone.utc) - WINDOW

    out: dict = {
        "roles": sorted(roles),
        "window_hours": int(WINDOW.total_seconds() // 3600),
        # The admin sees everyone's request counts; others see their own.
        "activity": _activity(session, since, None if is_admin else subject.id),
    }
    if is_admin:
        out["pending_approvals"] = (
            session.query(func.count()).select_from(Approval).filter(Approval.state == "pending").scalar()
        )
        out["users"] = session.query(func.count()).select_from(User).scalar()
        out["departments"] = [
            {"name": d.name, "quota_per_min": d.quota_per_min, "tools": d.tools}
            for d in session.query(Department).order_by(Department.name).all()
        ]
    if is_admin or "account" in roles:
        out["inbox_queued"] = (
            session.query(func.count())
            .select_from(DepartmentTask)
            .filter(DepartmentTask.department == "account", DepartmentTask.state == "queued")
            .scalar()
        )
    if roles & {"hr", "admin", "account"}:
        open_reports = session.query(Report).filter(Report.state == "open").all()
        out["open_reports"] = sum(1 for r in open_reports if roles & set(r.notified))
    return out
