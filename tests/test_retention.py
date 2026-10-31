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


def test_purge_old_artifacts_keeps_text(session):
    now = dt.datetime(2026, 10, 31, 12, 0, tzinfo=dt.timezone.utc)
    old = ChatHistory(
        id="old", owner_id="u1", thread_id="t1", text="hello from four days ago",
        artifacts=[{"type": "image", "ref": "img1"}], created_at=now - dt.timedelta(days=4),
    )
    fresh = ChatHistory(
        id="new", owner_id="u1", thread_id="t2", text="recent",
        artifacts=[{"type": "image", "ref": "img2"}], created_at=now - dt.timedelta(hours=2),
    )
    session.add_all([old, fresh])
    session.commit()

    purged = purge_expired(session, now=now)
    assert purged == 1

    session.refresh(old)
    session.refresh(fresh)
    # old: artifacts gone, text retained
    assert old.artifacts == [] and old.artifacts_purged is True
    assert old.text == "hello from four days ago"
    # fresh: untouched
    assert fresh.artifacts and fresh.artifacts_purged is False


def test_purge_is_idempotent(session):
    now = dt.datetime(2026, 10, 31, 12, 0, tzinfo=dt.timezone.utc)
    session.add(ChatHistory(
        id="x", owner_id="u1", thread_id="t", text="t",
        artifacts=[{"ref": "a"}], created_at=now - dt.timedelta(days=5),
    ))
    session.commit()
    assert purge_expired(session, now=now) == 1
    assert purge_expired(session, now=now) == 0  # already purged
