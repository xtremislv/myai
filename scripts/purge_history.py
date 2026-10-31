"""Purge non-text chat-history artifacts older than the retention window (3 days).

Run on a schedule (cron / the schedule skill), e.g. hourly:
    python -m scripts.purge_history
"""
from __future__ import annotations

from app.db.session import SessionLocal, init_db
from app.request_manager.retention import purge_expired


def main() -> None:
    init_db()
    with SessionLocal() as s:
        n = purge_expired(s)
    print(f"purged artifacts from {n} chat-history row(s)")


if __name__ == "__main__":
    main()
