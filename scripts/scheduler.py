"""Background jobs: deliver due reminders, send morning mail digests, and clean up.

    python -m scripts.scheduler          # run forever (start_native.sh starts it)
    python -m scripts.scheduler --once   # one pass of every job, then exit

Every TICK seconds: reminders that came due become notifications (and emails if asked).
Every DIGEST_EVERY seconds: digests for people whose digest hour has come.
Every hour: emptied trash, old undo copies and old read notifications are removed.
"""
from __future__ import annotations

import argparse
import logging
import time

from app.db.session import SessionLocal
from app.reminders import digest, service
from app.storage import deptfiles, userfiles

TICK = 30
DIGEST_EVERY = 300
HOUSEKEEPING_EVERY = 3600

logger = logging.getLogger("scheduler")


def _job(name: str, fn) -> None:
    try:
        with SessionLocal() as session:
            n = fn(session)
        if n:
            logger.info("%s: %s", name, n)
    except Exception:  # noqa: BLE001 - keep the loop alive; the next tick retries
        logger.exception("%s failed", name)


def reminders(session) -> int:
    return service.deliver_due(session)


def digests(session) -> int:
    return digest.run_due(session)


def housekeeping(session) -> int:
    return (
        userfiles.purge_trash(session)
        + service.prune_notifications(session)
        + deptfiles.purge_versions()
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.once:
        for name, fn in (("reminders", reminders), ("digests", digests), ("housekeeping", housekeeping)):
            _job(name, fn)
        return

    last_digest = last_housekeeping = 0.0
    while True:
        started = time.monotonic()
        _job("reminders", reminders)
        if started - last_digest >= DIGEST_EVERY:
            _job("digests", digests)
            last_digest = started
        if started - last_housekeeping >= HOUSEKEEPING_EVERY:
            _job("housekeeping", housekeeping)
            last_housekeeping = started
        time.sleep(max(1.0, TICK - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
