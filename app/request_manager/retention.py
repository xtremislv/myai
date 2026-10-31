"""Chat-history retention: keep text, purge non-text artifacts after 3 days."""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from app.db.session import ChatHistory

RETENTION_DAYS = 3


def purge_expired(session: Session, now: dt.datetime | None = None, days: int = RETENTION_DAYS) -> int:
    """Clear `artifacts` on history rows older than `days` (text is kept).
    Returns the number of rows purged."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=days)
    rows = (
        session.query(ChatHistory)
        .filter(ChatHistory.created_at < cutoff, ChatHistory.artifacts_purged.is_(False))
        .all()
    )
    for row in rows:
        row.artifacts = []
        row.artifacts_purged = True
    session.commit()
    return len(rows)
