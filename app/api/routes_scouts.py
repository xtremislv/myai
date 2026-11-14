"""Scouts (app/scout.py): standing research tasks a department's members watch and steer.

Access follows the department's files: whoever may read the department's `Scout/` folder sees its scouts
and findings, whoever may write there changes settings, runs a round now and reviews findings. So a
scout is for the department's members only, like its other documents (no admin shortcut)."""
from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import scout as scouts
from app.acl.engine import ACLEngine
from app.acl.models import Action, Subject
from app.api.deps import current_subject
from app.db.session import Scout, ScoutFinding, ScoutRun, get_session
from app.departments.packs import DEFAULT_DEPARTMENTS
from app.storage import objects
from app.storage.deptfiles import key_for

router = APIRouter(prefix="/v1/scouts", tags=["scouts"])
STATUSES = ("new", "kept", "rejected", "below")


def _allowed(session: Session, subject: Subject, department: str, action: Action) -> bool:
    if department not in {d.name for d in DEFAULT_DEPARTMENTS} or department == "admin":
        return False
    try:
        ACLEngine(session).enforce(subject, action, objects.resource_for(key_for(f"{scouts.REPORT_FOLDER}/x.xlsx", department)))
        return True
    except PermissionError:
        return False


def _scout(session: Session, subject: Subject, scout_id: str, action: Action = Action.READ) -> Scout:
    row = session.get(Scout, scout_id)
    if row is None or not _allowed(session, subject, row.department, Action.READ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such scout")
    if action != Action.READ and not _allowed(session, subject, row.department, action):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only the department's members can change this scout")
    return row


def _departments(session: Session, subject: Subject) -> list[str]:
    return [r for r in subject.roles if _allowed(session, subject, r, Action.WRITE)]


class RunOut(BaseModel):
    id: str
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    status: str
    trigger: str
    queries: list[str]
    candidates: int
    judged: int
    added: int
    notes: str


class ScoutOut(BaseModel):
    id: str
    name: str
    department: str
    enabled: bool
    settings: dict[str, Any]
    running: bool
    last_run_at: dt.datetime | None
    next_run_at: dt.datetime | None
    counts: dict[str, int]
    file: str
    can_edit: bool
    runs: list[RunOut] = []


class FindingOut(BaseModel):
    id: str
    name: str
    summary: str
    why: str
    scores: dict[str, float]
    total: float
    status: str
    video_url: str
    video_title: str
    video_views: int
    product_url: str
    price: str
    found_at: dt.datetime | None
    reviewed_by: str


def _out(session: Session, subject: Subject, row: Scout, runs: int = 0) -> ScoutOut:
    counts = {s: 0 for s in STATUSES}
    for (st,) in session.query(ScoutFinding.status).filter(ScoutFinding.scout_id == row.id):
        counts[st] = counts.get(st, 0) + 1
    recent = (session.query(ScoutRun).filter(ScoutRun.scout_id == row.id).order_by(ScoutRun.started_at.desc())
              .limit(runs).all() if runs else [])
    return ScoutOut(
        id=row.id, name=row.name, department=row.department, enabled=row.enabled,
        settings={k: v for k, v in scouts.clean_settings(row.settings).items()},
        running=row.running_since is not None, last_run_at=row.last_run_at,
        next_run_at=row.next_run_at if row.enabled else None, counts=counts,
        file=key_for(scouts.report_path(row), row.department),
        can_edit=_allowed(session, subject, row.department, Action.WRITE),
        runs=[RunOut(id=r.id, started_at=r.started_at, finished_at=r.finished_at, status=r.status, trigger=r.trigger,
                     queries=list(r.queries or []), candidates=r.candidates, judged=r.judged, added=r.added,
                     notes=r.notes) for r in recent])


@router.get("", response_model=list[ScoutOut])
def list_scouts(subject: Subject = Depends(current_subject), session: Session = Depends(get_session)):
    rows = session.query(Scout).order_by(Scout.created_at).all()
    return [_out(session, subject, r) for r in rows if _allowed(session, subject, r.department, Action.READ)]


@router.get("/defaults")
def defaults(subject: Subject = Depends(current_subject), session: Session = Depends(get_session)):
    """What a new scout starts with, and the departments the caller can make one for."""
    return {"settings": scouts.DEFAULTS, "sources": list(scouts.SOURCES), "limits": scouts.LIMITS,
            "departments": _departments(session, subject)}


class ScoutIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    department: str
    settings: dict[str, Any] | None = None


@router.post("", response_model=ScoutOut, status_code=status.HTTP_201_CREATED)
def create_scout(body: ScoutIn, subject: Subject = Depends(current_subject), session: Session = Depends(get_session)):
    if body.department not in _departments(session, subject):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you can only make scouts for your own department")
    try:
        row = scouts.create(session, body.department, body.name, subject.id, body.settings)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
    return _out(session, subject, row)


@router.get("/{scout_id}", response_model=ScoutOut)
def get_scout(scout_id: str, subject: Subject = Depends(current_subject), session: Session = Depends(get_session)):
    return _out(session, subject, _scout(session, subject, scout_id), runs=30)


class ScoutPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None
    settings: dict[str, Any] | None = None


@router.put("/{scout_id}", response_model=ScoutOut)
def update_scout(scout_id: str, body: ScoutPatch, subject: Subject = Depends(current_subject),
                 session: Session = Depends(get_session)):
    row = _scout(session, subject, scout_id, Action.WRITE)
    if body.settings is not None:
        try:
            merged = scouts.clean_settings({**scouts.clean_settings(row.settings), **body.settings})
        except ValueError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
        if merged["interval_minutes"] != scouts.clean_settings(row.settings)["interval_minutes"] and row.last_run_at:
            row.next_run_at = row.last_run_at + dt.timedelta(minutes=merged["interval_minutes"])
        row.settings = merged
        scouts.rescore(session, row)  # judged findings follow the new criteria at once
    if body.name is not None:
        row.name = " ".join(body.name.split())
    if body.enabled is not None:
        row.enabled = body.enabled
        if body.enabled and row.next_run_at is None:
            row.next_run_at = dt.datetime.now(dt.timezone.utc)
    row.updated_by = subject.id
    row.updated_at = dt.datetime.now(dt.timezone.utc)
    session.commit()
    if body.settings is not None:
        try:
            scouts.save_report(session, row)
        except Exception:  # noqa: BLE001 - the next round writes it anyway
            pass
    return _out(session, subject, row, runs=30)


@router.post("/{scout_id}/run", response_model=ScoutOut)
def run_now(scout_id: str, subject: Subject = Depends(current_subject), session: Session = Depends(get_session)):
    """Start a round within half a minute (the scheduler picks it up), even while paused."""
    row = _scout(session, subject, scout_id, Action.WRITE)
    if row.running_since is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "a round is already running")
    row.settings = {**(row.settings or {}), "_manual": True}
    row.next_run_at = dt.datetime.now(dt.timezone.utc)
    if not row.enabled:  # one round; it stays paused afterwards
        row.enabled = True
        row.settings = {**row.settings, "_pause_after": True}
    session.commit()
    return _out(session, subject, row, runs=30)


@router.delete("/{scout_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_scout(scout_id: str, subject: Subject = Depends(current_subject), session: Session = Depends(get_session)):
    row = _scout(session, subject, scout_id, Action.WRITE)
    if row.running_since is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "wait for the running round to finish, or pause it first")
    session.query(ScoutFinding).filter(ScoutFinding.scout_id == row.id).delete()
    session.query(ScoutRun).filter(ScoutRun.scout_id == row.id).delete()
    session.delete(row)
    session.commit()


@router.get("/{scout_id}/findings", response_model=list[FindingOut])
def findings(scout_id: str, state: str = "found", limit: int = 200, subject: Subject = Depends(current_subject),
             session: Session = Depends(get_session)):
    """state: found (new + kept), or one of new, kept, rejected, below, or all."""
    _scout(session, subject, scout_id)
    q = session.query(ScoutFinding).filter(ScoutFinding.scout_id == scout_id)
    if state == "found":
        q = q.filter(ScoutFinding.status.in_(("new", "kept")))
    elif state in STATUSES:
        q = q.filter(ScoutFinding.status == state)
    elif state != "all":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown state")
    q = q.order_by(ScoutFinding.found_at.desc()) if state != "found" else q.order_by(
        ScoutFinding.total.desc(), ScoutFinding.found_at.desc())
    return [FindingOut(id=f.id, name=f.name, summary=f.summary, why=f.why, scores=dict(f.scores or {}), total=f.total,
                       status=f.status, video_url=f.video_url, video_title=f.video_title, video_views=f.video_views,
                       product_url=f.product_url, price=f.price, found_at=f.found_at, reviewed_by=f.reviewed_by)
            for f in q.limit(max(1, min(limit, 1000)))]


class Review(BaseModel):
    status: Literal["new", "kept", "rejected"]


@router.patch("/{scout_id}/findings/{finding_id}", response_model=FindingOut)
def review(scout_id: str, finding_id: str, body: Review, subject: Subject = Depends(current_subject),
           session: Session = Depends(get_session)):
    """Keep or reject a finding (a rejected one stays known, so it is never suggested again)."""
    _scout(session, subject, scout_id, Action.WRITE)
    f = session.get(ScoutFinding, finding_id)
    if f is None or f.scout_id != scout_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such finding")
    f.status = body.status
    f.reviewed_by = subject.id
    session.commit()
    return FindingOut(id=f.id, name=f.name, summary=f.summary, why=f.why, scores=dict(f.scores or {}), total=f.total,
                      status=f.status, video_url=f.video_url, video_title=f.video_title, video_views=f.video_views,
                      product_url=f.product_url, price=f.price, found_at=f.found_at, reviewed_by=f.reviewed_by)
