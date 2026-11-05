"""Background jobs for work too long for a chat reply (e.g. a sheet of 1,000 summarized emails).

A job runs in a thread of the gateway with a copy of the caller's context (so it can save to the
person's encrypted files with the key they unlocked), and ends with a notification in the bell.
Jobs live in memory: a gateway restart loses a running job, and the person is told when they ask.

Every job ends with a notification in the bell (shown as a browser pop-up while the app is open).
A job can also ask (``Alerts``) for the result to be emailed and for a reminder to look at it later.
"""
from __future__ import annotations

import contextvars
import datetime as dt
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

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
