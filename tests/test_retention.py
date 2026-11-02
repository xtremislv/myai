"""Chat-history retention: non-text artifacts purged after 3 days, text kept."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base, ChatHistory
from app.request_manager.retention import purge_expired


@pytest.fixture
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as s:
        yield s


NOW = dt.datetime(2026, 10, 31, 12, 0, tzinfo=dt.timezone.utc)


def _row(row_id, age, *refs):
    return ChatHistory(
        id=row_id, owner_id="u1", thread_id="t",
        artifacts=[{"type": "image", "ref": r} for r in refs], created_at=NOW - age,
    )


def test_attachments_older_than_three_days_are_deleted_from_storage(session):
    old = _row("old", dt.timedelta(days=4), "chat/a.png", "chat/b.pdf")
    fresh = _row("new", dt.timedelta(hours=2), "chat/c.png")
    session.add_all([old, fresh])
    session.commit()
    deleted = []

    assert purge_expired(session, now=NOW, delete_object=deleted.append) == 1

    assert deleted == ["chat/a.png", "chat/b.pdf"]
    session.refresh(old)
    session.refresh(fresh)
    assert old.artifacts == [] and old.artifacts_purged is True
    assert fresh.artifacts and fresh.artifacts_purged is False


def test_purge_is_idempotent(session):
    session.add(_row("x", dt.timedelta(days=5), "chat/a.png"))
    session.commit()
    deleted = []
    assert purge_expired(session, now=NOW, delete_object=deleted.append) == 1
    assert purge_expired(session, now=NOW, delete_object=deleted.append) == 0
    assert deleted == ["chat/a.png"]


def test_failed_deletion_is_retried_and_does_not_block_other_rows(session):
    session.add_all([
        _row("bad", dt.timedelta(days=5), "chat/bad.png"),
        _row("good", dt.timedelta(days=5), "chat/good.png"),
    ])
    session.commit()

    def _flaky(key):
        if key == "chat/bad.png":
            raise ConnectionError("minio is down")

    assert purge_expired(session, now=NOW, delete_object=_flaky) == 1
    assert session.get(ChatHistory, "bad").artifacts_purged is False
    assert purge_expired(session, now=NOW, delete_object=lambda key: None) == 1
    assert session.get(ChatHistory, "bad").artifacts_purged is True


def test_chat_history_has_no_text_column():
    """Message text lives encrypted in checkpoints; a plain copy here would undo that."""
    assert "text" not in ChatHistory.__table__.columns
