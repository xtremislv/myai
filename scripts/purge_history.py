"""Delete chat attachments older than the retention window (3 days). Text is kept.

docker-compose.yml runs this hourly in the `retention` service. To run it once:
    python -m scripts.purge_history
"""
from __future__ import annotations

from app.db.session import SessionLocal
from app.request_manager.retention import purge_expired


def main() -> None:
    with SessionLocal() as s:
        n = purge_expired(s)
    print(f"purged artifacts from {n} chat-history row(s)")


if __name__ == "__main__":
    main()
