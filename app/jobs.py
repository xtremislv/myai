"""Background jobs for work too long for a chat reply (e.g. a sheet of 1,000 summarized emails).

A job runs in a thread of the gateway with a copy of the caller's context (so it can save to the
person's encrypted files with the key they unlocked), and ends with a notification in the bell.
Jobs live in memory: a gateway restart loses a running job. So each running job leaves a marker on
disk (its owner and its title, encrypted with the notes key), removed when it ends; at startup any
marker left means the job was cut off, and its owner is notified to start it again.

Every job ends with a notification in the bell (shown as a browser pop-up while the app is open).
A job can also ask (``Alerts``) for the result to be emailed and for a reminder to look at it later.
"""
from __future__ import annotations

import contextvars
import datetime as dt
import json
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_PER_PERSON = 1   # running at once
MAX_RUNNING = 3      # across everyone: the GPU is shared with live chats
KEEP_FINISHED = 20   # finished jobs remembered per person


@dataclass
class Job:
    id: str
    owner: str
    title: str
    status: str = "running"  # running | done | failed
    progress: str = "starting"
    result: str = ""
    started: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    finished: dt.datetime | None = None


_jobs: dict[str, Job] = {}
_lock = threading.Lock()


class Busy(Exception):
    pass


@dataclass(frozen=True)
class Alerts:
    """What happens when a job ends, besides the bell notification."""
    email: bool = False      # also email the result (from the notification mailbox)
    remind_at: str = ""      # also set a reminder to look at it, e.g. "tomorrow 9am" ("" = none)


def _markers() -> Path:
    from app.config import get_settings

    return Path(get_settings().work_dir) / "jobs"


def _mark(job: Job) -> None:
    """Note a running job on disk, so a restart that kills it can be reported (best effort)."""
    try:
        from app.db.session import SessionLocal
        from app.vault import notes

        with SessionLocal() as session:
            title = notes.encrypt(session, job.title, f"job:{job.id}")
        folder = _markers()
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        (folder / f"{job.id}.json").write_text(json.dumps({"id": job.id, "owner": job.owner, "title": title}))
    except Exception:  # noqa: BLE001 - the job runs anyway
        logger.info("job marker not written", exc_info=True)


def _unmark(job: Job) -> None:
    try:
        (_markers() / f"{job.id}.json").unlink(missing_ok=True)
    except OSError:
        pass


def report_interrupted() -> int:
    """At startup: tell the owner of every job a restart cut off. Returns how many."""
    from app.db.session import SessionLocal, User
    from app.reminders import service
    from app.vault import notes

    folder = _markers()
    if not folder.is_dir():
        return 0
    count = 0
    for path in folder.glob("*.json"):
        try:
            data = json.loads(path.read_text())
            with SessionLocal() as session:
                if session.get(User, data["owner"]) is not None:
                    title = notes.decrypt(session, data["title"], f"job:{data['id']}")
                    service.notify(session, data["owner"], "job", f"Interrupted: {title}",
                                   "The server restarted while this was running, so it stopped before it "
                                   "finished. Ask the assistant to start it again.")
                    count += 1
        except Exception:  # noqa: BLE001 - one bad marker must not stop the others
            logger.warning("interrupted job not reported", exc_info=True)
        path.unlink(missing_ok=True)
    return count


def running(owner: str | None = None) -> list[Job]:
    with _lock:
        return [j for j in _jobs.values() if j.status == "running" and (owner is None or j.owner == owner)]


def of(owner: str) -> list[Job]:
    with _lock:
        return sorted((j for j in _jobs.values() if j.owner == owner), key=lambda j: j.started, reverse=True)


def start(owner: str, title: str, work: Callable[[Callable[[str], None]], str],
          notify: Callable[[str, str, str], None] | None = None, alerts: Alerts | None = None) -> Job:
    """Run work(progress) in the background. It returns the text of the result; the person is
    notified with it (or with the failure) when it ends."""
    with _lock:
        mine = [j for j in _jobs.values() if j.status == "running" and j.owner == owner]
        if len(mine) >= MAX_PER_PERSON:
            raise Busy(f"'{mine[0].title}' is still running ({mine[0].progress}). Wait for it to finish.")
        if sum(j.status == "running" for j in _jobs.values()) >= MAX_RUNNING:
            raise Busy("the server is already running its maximum of background jobs. Try again in a few minutes.")
        job = Job(id=uuid.uuid4().hex[:12], owner=owner, title=title)
        _jobs[job.id] = job
    _mark(job)
    if notify is None:
        notify = (lambda o, t, b: _notify(o, t, b, alerts)) if alerts else (lambda o, t, b: _notify(o, t, b))

    def progress(text: str) -> None:
        job.progress = text

    def run() -> None:
        from app.llm.governor import background

        background.set(True)  # this job's model calls give way to live chats
        try:
            job.result = work(progress)
            job.status = "done"
            notify(owner, f"Ready: {title}", job.result)
        except Exception as e:  # noqa: BLE001 - reported to the person, not raised
            logger.exception("background job failed")
            job.status, job.result = "failed", f"{type(e).__name__}: {str(e)[:300]}"
            notify(owner, f"Couldn't finish: {title}", f"The job stopped: {job.result}. Ask the assistant to try again.")
        finally:
            job.finished = dt.datetime.now(dt.timezone.utc)
            _unmark(job)
            _prune(owner)

    threading.Thread(target=contextvars.copy_context().run, args=(run,), daemon=True, name=f"job-{job.id}").start()
    return job


def _prune(owner: str) -> None:
    with _lock:
        done = [j for j in _jobs.values() if j.owner == owner and j.status != "running"]
        for old in sorted(done, key=lambda j: j.started)[:-KEEP_FINISHED]:
            _jobs.pop(old.id, None)


def _notify(owner: str, title: str, body: str, alerts: Alerts | None = None) -> None:
    from app.db.session import SessionLocal, User
    from app.reminders import service

    with SessionLocal() as session:
        service.notify(session, owner, "job", title, body)
        if alerts is None:
            return
        if alerts.email:
            try:
                from app.reminders import delivery

                user = session.get(User, owner)
                if user is not None and not delivery.email_person(session, user, title, body):
                    service.notify(session, owner, "job", "Couldn't email the result",
                                   "No email address or notification mailbox is set up. The result is above.")
            except Exception:  # noqa: BLE001 - the bell notification already went out
                logger.warning("job result email failed", exc_info=True)
        if alerts.remind_at.strip() and title.startswith("Ready"):
            try:
                service.create(session, owner, alerts.remind_at, f"Look at: {title.removeprefix('Ready: ')}")
            except Exception:  # noqa: BLE001
                logger.warning("job reminder not set", exc_info=True)
                service.notify(session, owner, "job", "Couldn't set the reminder",
                               f"'{alerts.remind_at}' isn't a time I understand. Set one yourself if you need it.")
