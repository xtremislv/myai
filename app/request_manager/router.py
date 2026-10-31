"""Request Manager — the governed entrypoint in front of the orchestrator.

handle_request() runs the control-plane pipeline:
  1. classify the request to a department
  2. authorize (subject must hold the department role; admin may act for any)
  3. quota check (per-department, per-minute)
  4. critical check -> park for approval instead of executing
  5. execute the department's agent graph
  6. emit per-department metrics + audit, return a structured result
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.acl.models import Subject
from app.db.session import AuditLog, Department
from app.observability import (
    rm_approvals_total,
    rm_critical_gated_total,
    rm_quota_rejections_total,
    rm_request_latency_seconds,
    rm_requests_total,
    tracer,
)
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


def run_graph(subject: Subject, req: RMRequest, department: str) -> str:
    """Default runner: execute the orchestrator (single or team graph)."""
    # Lazy import so unit tests can inject a fake runner without Postgres/LLM.
    from app.orchestrator.graph import compiled_graph
    from app.orchestrator.multi_graph import compiled_team_graph

    initial = {
        "messages": [{"role": "user", "content": req.message}],
        "subject_id": subject.id,
        "subject_roles": list(subject.roles),
        "scratchpad": {"department": department},
    }
    config = {"configurable": {"thread_id": req.thread_id}}
    cm = compiled_team_graph() if req.mode == "team" else compiled_graph()
    with cm as graph:
        final = graph.invoke(initial, config=config)
    for msg in reversed(final.get("messages", [])):
        if msg.get("role") == "assistant":
            return msg.get("content", "")
    return ""


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

        # 4. critical -> approval
        critical, why = approvals_store.is_critical(session, department, req.message)
        if critical:
            rm_critical_gated_total.labels(department=department).inc()
            approval = approvals_store.create(
                session,
                department=department,
                subject_id=subject.id,
                message=req.message,
                thread_id=req.thread_id,
                mode=req.mode,
            )
            rm_approvals_total.labels(department=department, state="pending").inc()
            _finish(session, subject, department, "pending_approval", why, start)
            return RMResult("pending_approval", department, approval_id=approval.id, reason=why)

        # 5. execute
        try:
            reply = runner(subject, req, department)
        except Exception as e:  # noqa: BLE001
            _finish(session, subject, department, "error", str(e), start)
            return RMResult("error", department, reason=str(e))

        _finish(session, subject, department, "ok", cls.reason, start)
        return RMResult("ok", department, reply=reply, reason=cls.reason)


def execute_approved(session: Session, approval, *, runner=run_graph) -> str:
    """Run a now-approved request. Caller has verified state == 'approved'."""
    subject = Subject(id=approval.subject_id, roles=(approval.department,))
    req = RMRequest(message=approval.message, thread_id=approval.thread_id, mode=approval.mode)
    rm_approvals_total.labels(department=approval.department, state="executed").inc()
    return runner(subject, req, approval.department)


def execute_task(session: Session, task, processor_id: str, *, runner=run_graph) -> str:
    """Run a forwarded inbox task under the department role (the processor acts
    as a member of that department)."""
    subject = Subject(id=processor_id, roles=(task.department,))
    req = RMRequest(message=task.message, thread_id=task.thread_id, mode=task.mode)
    return runner(subject, req, task.department)


def _finish(session, subject, department, outcome, reason, start) -> None:
    rm_requests_total.labels(department=department or "none", outcome=outcome).inc()
    if department:
        rm_request_latency_seconds.labels(department=department).observe(time.perf_counter() - start)
    _audit(session, subject, department, outcome, reason)
