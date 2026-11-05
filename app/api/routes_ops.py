"""Server alerts for the admin: Prometheus sends its alerts here, and they appear under the
admin's bell (and by email, when the notification mailbox `mail/notify` is set up), once when
they start and once when they resolve.

Prometheus speaks the Alertmanager API, so this answers POST <prefix>/api/v2/alerts with a
list of alerts. deploy/native/prometheus.yml points `alerting` at
http://127.0.0.1:8000/v1/ops/alerts with the bearer token ALERT_WEBHOOK_TOKEN from .env.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import OpsAlert, get_session
from app.reminders import delivery, service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ops", tags=["ops"])


class AlertIn(BaseModel):
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: dt.datetime | None = None  # noqa: N815 - Alertmanager's field names
    endsAt: dt.datetime | None = None  # noqa: N815


def _authorized(authorization: str | None) -> None:
    token = get_settings().alert_webhook_token
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(given.encode(), token.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad alert token")


def _fingerprint(labels: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(sorted(labels.items())).encode()).hexdigest()[:40]


def record(session: Session, alerts: list[AlertIn], now: dt.datetime | None = None) -> int:
    """Store each alert's state and notify the admin of changes. Returns notifications sent."""
    now = now or dt.datetime.now(dt.timezone.utc)
    sent = 0
    for alert in alerts:
        name = alert.labels.get("alertname", "alert")[:128]
        ends = alert.endsAt
        if ends is not None and ends.tzinfo is None:
            ends = ends.replace(tzinfo=dt.timezone.utc)
        resolved = ends is not None and ends.year > 1 and ends <= now
        fp = _fingerprint(alert.labels)
        row = session.get(OpsAlert, fp)
        summary = (alert.annotations.get("summary") or alert.annotations.get("description") or "")[:512]
        severity = alert.labels.get("severity", "")[:32]
        before = row.state if row is not None else None
        if row is None:
            row = OpsAlert(fingerprint=fp, name=name, started_at=alert.startsAt)
            session.add(row)
        row.summary, row.severity, row.last_seen = summary, severity, now
        row.state = "resolved" if resolved else "firing"
        session.commit()
        if row.state == "firing" and before != "firing":
            title = f"Server alert ({severity or 'warning'}): {name}"
        elif row.state == "resolved" and before == "firing":
            title = f"Resolved: {name}"
        else:
            continue
        sent += service.notify_admins(session, "alert", title, summary)
        _email(session, title, summary)
    return sent


def _email(session: Session, title: str, summary: str) -> None:
    try:
        delivery.email_admins(session, title, summary or title)
    except Exception:  # noqa: BLE001 - the bell notice is already saved; mail is best effort
        logger.warning("could not email alert %r", title, exc_info=True)


@router.post("/alerts/api/v2/alerts")
def receive_alerts(
    alerts: list[AlertIn], authorization: str | None = Header(default=None), session: Session = Depends(get_session),
) -> dict:
    _authorized(authorization)
    return {"notified": record(session, alerts)}
