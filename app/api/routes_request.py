"""Governed request entrypoint + approvals API.

`POST /v1/request` runs the Request Manager pipeline (classify → authorize →
quota → execute). A critical tool call pauses the run and returns
`pending_approval`; an admin then approves/denies via `/v1/approvals`, which
resumes the run from its checkpoint.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.acl.models import Subject
from app.api.deps import current_subject, require_admin
from app.db.session import DepartmentTask, Report, User, get_session
from app.request_manager import approvals as approvals_store
from app.request_manager.router import RMRequest, execute_task, handle_request, resolve_approval

router = APIRouter(tags=["request-manager"])


class RequestIn(BaseModel):
    message: str
    thread_id: str = Field(min_length=1, max_length=128)
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
    if result.outcome in ("denied_quota", "denied_busy"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, result.reason)
    return RequestOut(**result.__dict__)


def _usernames(session: Session, user_ids: set[str]) -> dict[str, str]:
    rows = session.query(User.id, User.username).filter(User.id.in_(user_ids)).all()
    return dict(rows)


# ---- Approvals (admin only) ----
class ApprovalOut(BaseModel):
    id: str
    department: str
    subject_id: str
    subject_name: str
    message: str
    thread_id: str
    mode: str
    state: str
    tool: str
    tool_args: dict


class DecisionIn(BaseModel):
    reason: str = ""


@router.get("/v1/approvals", response_model=list[ApprovalOut])
def list_approvals(
    _admin: Subject = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[ApprovalOut]:
    rows = approvals_store.list_pending(session)
    names = _usernames(session, {a.subject_id for a in rows})
    return [
        ApprovalOut(
            id=a.id, department=a.department, subject_id=a.subject_id,
            subject_name=names.get(a.subject_id, a.subject_id),
            message=a.message, thread_id=a.thread_id, mode=a.mode, state=a.state,
            tool=a.tool, tool_args=a.tool_args,
        )
        for a in rows
    ]


@router.post("/v1/approvals/{approval_id}/approve", response_model=RequestOut)
def approve(
    approval_id: str,
    admin: Subject = Depends(require_admin),
    session: Session = Depends(get_session),
    body: DecisionIn | None = None,
) -> RequestOut:
    reason = body.reason if body else ""
    row, won = approvals_store.decide(session, approval_id, approve=True, decided_by=admin.id, reason=reason)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
    if not won:
        raise HTTPException(status.HTTP_409_CONFLICT, f"already {row.state}")
    return RequestOut(**resolve_approval(session, row).__dict__)


@router.post("/v1/approvals/{approval_id}/deny", response_model=RequestOut)
def deny(
    approval_id: str,
    admin: Subject = Depends(require_admin),
    session: Session = Depends(get_session),
    body: DecisionIn | None = None,
) -> RequestOut:
    reason = body.reason if body else ""
    row, won = approvals_store.decide(session, approval_id, approve=False, decided_by=admin.id, reason=reason)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
    if not won:
        raise HTTPException(status.HTTP_409_CONFLICT, f"already {row.state}")
    return RequestOut(**resolve_approval(session, row).__dict__)


# ---- Department inbox (forwarded tasks, e.g. orders/bills to accounts) ----
class TaskOut(BaseModel):
    id: str
    department: str
    requester_id: str
    requester_name: str
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
    names = _usernames(session, {t.requester_id for t in tasks})
    return [
        TaskOut(
            id=t.id, department=t.department, requester_id=t.requester_id,
            requester_name=names.get(t.requester_id, t.requester_id),
            kind=t.kind, message=t.message, state=t.state,
        )
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
    # Atomic claim, so two members processing the same task cannot both run it.
    claimed = (
        session.query(DepartmentTask)
        .filter(DepartmentTask.id == task_id, DepartmentTask.state == "queued")
        .update({"state": "processing"}, synchronize_session=False)
    )
    session.commit()
    session.refresh(task)
    if not claimed:
        raise HTTPException(status.HTTP_409_CONFLICT, f"already {task.state}")
    return RequestOut(**execute_task(session, task, subject.id).__dict__)


# ---- Reports: NSFW flags (hr, admin) and finance notices (account) ----
class ReportOut(BaseModel):
    id: str
    kind: str
    subject_id: str
    subject_name: str
    message: str
    state: str


@router.get("/v1/reports", response_model=list[ReportOut])
def list_reports(
    subject: Subject = Depends(current_subject),
    session: Session = Depends(get_session),
) -> list[ReportOut]:
    roles = set(subject.roles)
    if not ({"hr", "admin", "account"} & roles):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "reports are visible to hr, admin and account only")
    rows = session.query(Report).filter(Report.state == "open").order_by(Report.created_at).all()
    visible = [r for r in rows if roles & set(r.notified)]
    names = _usernames(session, {r.subject_id for r in visible})
    return [
        ReportOut(
            id=r.id, kind=r.kind, subject_id=r.subject_id,
            subject_name=names.get(r.subject_id, r.subject_id), message=r.message, state=r.state,
        )
        for r in visible
    ]