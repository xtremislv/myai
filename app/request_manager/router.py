"""Request Manager — the governed entrypoint in front of the orchestrator.

handle_request() runs the control-plane pipeline:
  1. classify the request to a department
  2. authorize (subject must hold the department role; admin may act for any)
  3. quota check (per-department, per-minute)
  4. execute the department's agent graph; a critical tool call pauses the graph
     and parks an approval (see orchestrator.nodes.run_tool)
  5. emit per-department metrics + audit, return a structured result
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.acl.models import Subject
from app.db.session import Approval, AuditLog, Department, DepartmentTask, release_connection
from app.departments.packs import is_finance_call
from app.observability import (
    rm_approvals_total,
    rm_critical_gated_total,
    rm_quota_rejections_total,
    rm_request_latency_seconds,
    rm_requests_total,
    tracer,
)
from app.orchestrator import execution
from app.orchestrator.execution import PendingAction, scoped_thread
from app.request_manager import approvals as approvals_store
from app.request_manager import guards
from app.request_manager.classifier import classify
from app.request_manager.quota import QuotaLimiter, default_quota


@dataclass
class RMRequest:
    message: str
    thread_id: str
    mode: str = "single"
    department: str | None = None  # explicit hint


@dataclass
class RMResult:
    # ok | denied_auth | denied_quota | pending_approval | flagged_nsfw | forwarded | error
    outcome: str
    department: str | None = None
    reply: str | None = None
    approval_id: str | None = None
    task_id: str | None = None
    report_id: str | None = None
    reason: str = ""


def run_graph(subject: Subject, req: RMRequest, department: str) -> str | PendingAction:
    """Default runner: execute the orchestrator (single or team graph)."""
    return execution.run(subject, req.message, req.thread_id, req.mode, department)


def resume_graph(approval: Approval, approved: bool, reason: str) -> str | PendingAction:
    """Default resumer: continue the paused run an approval belongs to."""
    return execution.resume(
        approval.checkpoint_thread,
        approval.mode,
        approved=approved,
        reason=reason,
        tool=approval.tool,
        args=approval.tool_args,
    )


def _audit(session: Session, subject: Subject, department: str | None, outcome: str, reason: str) -> None:
    session.add(
        AuditLog(
            subject=subject.id,
            action="request",
            resource=f"dept:{department or 'none'}",
            decision=outcome,
            detail={"reason": reason, "department": department},
        )
    )
    session.commit()


def handle_request(
    session: Session,
    subject: Subject,
    req: RMRequest,
    *,
    quota: QuotaLimiter | None = None,
    runner=run_graph,
) -> RMResult:
    quota = quota or default_quota()
    start = time.perf_counter()

    with tracer.start_as_current_span("rm.handle_request") as span:
        span.set_attribute("rm.subject", subject.id)

        # 0a. NSFW guard — block and escalate to HR + Admin.
        nsfw, why = guards.is_nsfw(req.message)
        if nsfw:
            report = guards.report_nsfw(session, subject.id, req.message)
            _finish(session, subject, None, "flagged_nsfw", why, start)
            return RMResult("flagged_nsfw", report_id=report.id, reason=f"NSFW flagged ({why}); reported to hr, admin")

        # 0b. Intent routing — orders/products/services/bills go to accounts.
        kind = guards.detect_forward_intent(req.message)
        if kind:
            task = guards.forward_to_account(
                session, kind=kind, requester_id=subject.id,
                message=req.message, thread_id=req.thread_id, mode=req.mode,
            )
            _finish(session, subject, "account", "forwarded", f"{kind} -> account inbox", start)
            return RMResult("forwarded", department="account", task_id=task.id, reason=f"{kind} forwarded to accounts")

        # 1. classify
        cls = classify(subject, req.message, req.department)
        department = cls.department
        span.set_attribute("rm.department", department or "none")

        # 2. authorize
        if department is None:
            _finish(session, subject, None, "denied_auth", cls.reason, start)
            return RMResult("denied_auth", None, reason=cls.reason)

        dept_row: Department | None = session.get(Department, department)
        if dept_row is None:
            _finish(session, subject, department, "denied_auth", "unknown department", start)
            return RMResult("denied_auth", department, reason="unknown department")

        # 3. quota
        if not quota.allow(department, dept_row.quota_per_min):
            rm_quota_rejections_total.labels(department=department).inc()
            _finish(session, subject, department, "denied_quota", "rate limit exceeded", start)
            return RMResult("denied_quota", department, reason="rate limit exceeded")

        # 4. a conversation paused on an approval cannot take new work: running it
        # would discard the paused tool call.
        waiting = approvals_store.pending_for_thread(session, scoped_thread(subject.id, req.thread_id))
        if waiting is not None:
            why = "this conversation is waiting on an approval"
            _finish(session, subject, department, "pending_approval", why, start)
            return RMResult("pending_approval", department, approval_id=waiting.id, reason=why)

        # 5. execute. Accounts is told when the admin works in its department.
        if "admin" in subject.roles and department == "account":
            guards.notify_accounts(session, subject.id, req.message)
        release_connection(session)
        try:
            out = runner(subject, req, department)
        except Exception as e:  # noqa: BLE001
            _finish(session, subject, department, "error", str(e), start)
            return RMResult("error", department, reason=str(e))

        if isinstance(out, PendingAction):
            approval = _park(session, subject.id, department, req.message, req.thread_id, req.mode, out)
            why = f"{out.tool} requires approval"
            _finish(session, subject, department, "pending_approval", why, start)
            return RMResult("pending_approval", department, approval_id=approval.id, reason=why)

        _finish(session, subject, department, "ok", cls.reason, start)
        return RMResult("ok", department, reply=out, reason=cls.reason)


def _park(
    session: Session,
    subject_id: str,
    department: str,
    message: str,
    thread_id: str,
    mode: str,
    pending: PendingAction,
    task_id: str | None = None,
) -> Approval:
    rm_critical_gated_total.labels(department=department).inc()
    rm_approvals_total.labels(department=department, state="pending").inc()
    return approvals_store.create(
        session,
        department=department,
        subject_id=subject_id,
        message=message,
        thread_id=thread_id,
        mode=mode,
        tool=pending.tool,
        tool_args=pending.args,
        checkpoint_thread=pending.checkpoint_thread,
        task_id=task_id,
    )


def resolve_approval(session: Session, approval: Approval, *, resumer=resume_graph) -> RMResult:
    """Resume the paused run after approval.state was set to approved or denied.

    A failed resume is recorded and never retried: the tool may have had a side
    effect before the failure.
    """
    approved = approval.state == "approved"
    dept = approval.department
    subject = Subject(id=approval.subject_id, roles=(dept,))
    task = session.get(DepartmentTask, approval.task_id) if approval.task_id else None
    rm_approvals_total.labels(department=dept, state=approval.state).inc()
    start = time.perf_counter()

    # With one admin, who may approve their own requests, this is the check on them.
    if is_finance_call(approval.tool, approval.tool_args):
        guards.notify_accounts(
            session,
            approval.decided_by or "unknown",
            f"{approval.decided_by} {approval.state} {approval.tool}({approval.tool_args}) "
            f"requested by {approval.subject_id}",
        )

    release_connection(session)
    try:
        out = resumer(approval, approved, approval.reason)
    except Exception as e:  # noqa: BLE001 - includes StaleApproval
        if approved:
            approval.state = "failed"
            approval.reason = str(e)[:512]
            if task is not None:
                task.state = "failed"
            session.commit()
            _finish(session, subject, dept, "error", str(e), start)
            return RMResult("error", dept, approval_id=approval.id, task_id=approval.task_id, reason=str(e))
        # A denied action did not run, so the denial stands even if the agent
        # could not be resumed to report it.
        out = ""

    # The agent went on to another critical call: that needs its own decision.
    if isinstance(out, PendingAction):
        next_approval = _park(
            session, approval.subject_id, dept, approval.message, approval.thread_id,
            approval.mode, out, task_id=approval.task_id,
        )
        why = f"{out.tool} requires approval"
        _finish(session, subject, dept, "pending_approval", why, start)
        return RMResult("pending_approval", dept, approval_id=next_approval.id,
                        task_id=approval.task_id, reason=why)

    if task is not None:
        task.state = "done" if approved else "denied"
        task.result = out
        session.commit()
    outcome = "ok" if approved else "denied"
    _finish(session, subject, dept, outcome, approval.reason, start)
    return RMResult(outcome, dept, reply=out, approval_id=approval.id, task_id=approval.task_id,
                    reason=approval.reason)


def execute_task(session: Session, task: DepartmentTask, processor_id: str, *, runner=run_graph) -> RMResult:
    """Run a claimed inbox task as a member of its department, on a thread of its
    own so the processor's conversations stay separate."""
    subject = Subject(id=processor_id, roles=(task.department,))
    req = RMRequest(message=task.message, thread_id=f"task-{task.id}", mode=task.mode)
    start = time.perf_counter()
    release_connection(session)
    try:
        out = runner(subject, req, task.department)
    except Exception as e:  # noqa: BLE001
        task.state = "queued"  # back in the queue; critical tools cannot have run unapproved
        session.commit()
        _finish(session, subject, task.department, "error", str(e), start)
        return RMResult("error", task.department, task_id=task.id, reason=str(e))

    if isinstance(out, PendingAction):
        approval = _park(
            session, processor_id, task.department, task.message, req.thread_id, task.mode, out,
            task_id=task.id,
        )
        task.state = "awaiting"
        session.commit()
        why = f"{out.tool} requires approval"
        _finish(session, subject, task.department, "pending_approval", why, start)
        return RMResult("pending_approval", task.department, approval_id=approval.id, task_id=task.id, reason=why)

    task.state = "done"
    task.result = out
    session.commit()
    _finish(session, subject, task.department, "ok", "inbox task", start)
    return RMResult("ok", task.department, reply=out, task_id=task.id)


def _finish(session, subject, department, outcome, reason, start) -> None:
    rm_requests_total.labels(department=department or "none", outcome=outcome).inc()
    if department:
        rm_request_latency_seconds.labels(department=department).observe(time.perf_counter() - start)
    _audit(session, subject, department, outcome, reason)
