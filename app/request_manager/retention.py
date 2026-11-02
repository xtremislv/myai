"""Chat retention: attachments are deleted after 3 days; text is kept indefinitely.

Text is not touched here at all. It lives encrypted in the checkpoints.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy.orm import Session

from app.db.session import ChatHistory

RETENTION_DAYS = 3

logger = logging.getLogger(__name__)


def _delete_stored_object(key: str) -> None:
    from app.storage.objects import remove_expired_object

    remove_expired_object(key)


def purge_expired(
    session: Session,
    now: dt.datetime | None = None,
    days: int = RETENTION_DAYS,
    delete_object=_delete_stored_object,
) -> int:
    """Delete attachments older than `days`. Returns how many chat rows were purged.

    A row whose deletion fails stays unpurged, so the next run retries it.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=days)
    rows = (
        session.query(ChatHistory)
        .filter(ChatHistory.created_at < cutoff, ChatHistory.artifacts_purged.is_(False))
        .all()
    )
    purged = 0
    for row in rows:
        try:
            for artifact in row.artifacts:
                delete_object(artifact["ref"])
        except Exception:  # noqa: BLE001
            logger.exception("could not delete attachments of chat row %s", row.id)
            continue
        row.artifacts = []
        row.artifacts_purged = True
        session.commit()
        purged += 1
    return purged
