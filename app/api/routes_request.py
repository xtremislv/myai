"""Governed request entrypoint + approvals API.

`POST /v1/request` runs the Request Manager pipeline (classify → authorize →
quota → critical/approval → execute). Critical requests return
`pending_approval`; an admin then approves/denies via `/v1/approvals`.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.acl.models import Subject
from app.api.deps import current_subject, require_admin
from app.db.session import DepartmentTask, get_session
from app.observability import rm_approvals_total
from app.request_manager import approvals as approvals_store
from app.request_manager.router import RMRequest, execute_approved, execute_task, handle_request

router = APIRouter(tags=["request-manager"])


class RequestIn(BaseModel):
    message: str
    thread_id: str
    mode: Literal["single", "team"] = "single"
    department: str | None = None  # optional explicit hint


class RequestOut(BaseModel):
    outcome: str
    department: str | None = None
    reply: str | None = None
    approval_id: str | None = None
    task_id: str | None = None
    report_id: str | None = None
    reason: str = ""


@router.post("/v1/request", response_model=RequestOut)
def submit_request(
    body: RequestIn,
    subject: Subject = Depends(current_subject),
    session: Session = Depends(get_session),
) -> RequestOut:
    req = RMRequest(message=body.message, thread_id=body.thread_id, mode=body.mode, department=body.department)
    result = handle_request(session, subject, req)
    if result.outcome == "denied_auth":
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason)
    if result.outcome == "denied_quota":
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, result.reason)
    return RequestOut(**result.__dict__)


# ---- Approvals (admin only) ----
class ApprovalOut(BaseModel):
    id: str
    department: str
    subject_id: str
    message: str
    thread_id: str
    mode: str
    state: str


class DecisionIn(BaseModel):
    reason: str = ""


@router.get("/v1/approvals", response_model=list[ApprovalOut])
def list_approvals(
    _admin: Subject = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[ApprovalOut]:
    return [
        ApprovalOut(
            id=a.id, department=a.department, subject_id=a.subject_id,
            message=a.message, thread_id=a.thread_id, mode=a.mode, state=a.state,
        )
        for a in approvals_store.list_pending(session)
    ]


@router.post("/v1/approvals/{approval_id}/approve", response_model=RequestOut)
def approve(
    approval_id: str,
    admin: Subject = Depends(require_admin),
    session: Session = Depends(get_session),
    body: DecisionIn | None = None,
) -> RequestOut:
    reason = body.reason if body else ""
    row = approvals_store.decide(session, approval_id, approve=True, decided_by=admin.id, reason=reason)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
    if row.state != "approved":
        raise HTTPException(status.HTTP_409_CONFLICT, f"already {row.state}")
    reply = execute_approved(session, row)
    return RequestOut(outcome="ok", department=row.department, reply=reply, approval_id=row.id)


@router.post("/v1/approvals/{approval_id}/deny", response_model=RequestOut)
def deny(
    approval_id: str,
    admin: Subject = Depends(require_admin),
    session: Session = Depends(get_session),
    body: DecisionIn | None = None,
) -> RequestOut:
    reason = body.reason if body else ""
    row = approvals_store.decide(session, approval_id, approve=False, decided_by=admin.id, reason=reason)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
    if row.state != "denied":
        raise HTTPException(status.HTTP_409_CONFLICT, f"already {row.state}")
    rm_approvals_total.labels(department=row.department, state="denied").inc()
    return RequestOut(outcome="denied", department=row.department, approval_id=row.id, reason="denied by admin")


# ---- Department inbox (forwarded tasks, e.g. orders/bills to accounts) ----
class TaskOut(BaseModel):
    id: str
    department: str
    requester_id: str
    kind: str
    message: str
    state: str


def _member_or_admin(subject: Subject, department: str) -> bool:
    return department in subject.roles or "admin" in subject.roles


@router.get("/v1/inbox", response_model=list[TaskOut])
def list_inbox(
    department: str,
    subject: Subject = Depends(current_subject),
    session: Session = Depends(get_session),
) -> list[TaskOut]:
    if not _member_or_admin(subject, department):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"not a member of '{department}'")
    tasks = (
        session.query(DepartmentTask)
        .filter(DepartmentTask.department == department, DepartmentTask.state == "queued")
        .order_by(DepartmentTask.created_at)
        .all()
    )
    return [
        TaskOut(id=t.id, department=t.department, requester_id=t.requester_id, kind=t.kind, message=t.message, state=t.state)
        for t in tasks
    ]


@router.post("/v1/inbox/{task_id}/process", response_model=RequestOut)
def process_task(
    task_id: str,
    subject: Subject = Depends(current_subject),
    session: Session = Depends(get_session),
) -> RequestOut:
    task = session.get(DepartmentTask, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
    if not _member_or_admin(subject, task.department):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"not a member of '{task.department}'")
    if task.state != "queued":
        raise HTTPException(status.HTTP_409_CONFLICT, f"already {task.state}")
    reply = execute_task(session, task, subject.id)
    task.state = "done"
    task.result = reply
    session.commit()
    return RequestOut(outcome="ok", department=task.department, reply=reply, task_id=task.id)


# ---- Reports (NSFW) — HR / Admin ----
class ReportOut(BaseModel):
    id: str
    kind: str
    subject_id: str
    message: str
    state: str


@router.get("/v1/reports", response_model=list[ReportOut])
def list_reports(
    subject: Subject = Depends(current_subject),
    session: Session = Depends(get_session),
) -> list[ReportOut]:
    if not ({"hr", "admin"} & set(subject.roles)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "reports are visible to hr and admin only")
    from app.db.session import Report

    rows = session.query(Report).filter(Report.state == "open").order_by(Report.created_at).all()
    return [ReportOut(id=r.id, kind=r.kind, subject_id=r.subject_id, message=r.message, state=r.state) for r in rows]