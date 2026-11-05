"""Chat requests still being worked on, so a person who left the chat (another page, another tab, a
reload, a dropped connection) sees on return that their request is still running, and its steps;
and one request at a time per chat, so two can't overwrite each other's messages.

Each run has an id. After it ends, its outcome ("done" or "failed", no content) is kept for a while,
so a page that lost the connection can tell "finished: the reply is saved" from "failed" and from
"unknown" (the server restarted and the run was lost with it: ask again).

In memory only: the question and the step lines live here while the request runs and are dropped
when it ends (the reply itself is saved in the conversation as usual). Nothing is logged.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

MAX_STEPS = 40
FORGET_AFTER = 3600  # a run that never reported its end (a crash) is forgotten after an hour
KEEP_OUTCOMES = 6 * 3600


@dataclass
class Running:
    owner: str
    thread: str
    message: str
    kind: str = "message"  # message | confirm
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started: float = field(default_factory=time.time)
    steps: list[str] = field(default_factory=list)
    done: bool = False


class Busy(RuntimeError):
    """This chat is already working on a request."""


_runs: dict[tuple[str, str], Running] = {}
_outcomes: dict[str, tuple[str, str, float]] = {}  # run id -> (owner, "done" | "failed", when)
_lock = threading.Lock()


def _alive(run: Running, now: float) -> bool:
    return not run.done and now - run.started <= FORGET_AFTER


def start(owner: str, thread: str, message: str, kind: str = "message") -> Running:
    """Register a run, or raise Busy if this chat already has one going."""
    now = time.time()
    with _lock:
        current = _runs.get((owner, thread))
        if current is not None and _alive(current, now):
            raise Busy("still working on the last message in this chat")
        run = Running(owner, thread, message, kind)
        _runs[(owner, thread)] = run
        for rid in [k for k, (_, _, t) in _outcomes.items() if now - t > KEEP_OUTCOMES]:
            del _outcomes[rid]
    return run


def step(run: Running, text: str) -> None:
    with _lock:
        if not run.done:
            run.steps = (run.steps + [text])[-MAX_STEPS:]


def finish(run: Running, ok: bool = True) -> None:
    with _lock:
        run.done = True
        _outcomes[run.id] = (run.owner, "done" if ok else "failed", time.time())
        if _runs.get((run.owner, run.thread)) is run:
            del _runs[(run.owner, run.thread)]


def get(owner: str, thread: str) -> Running | None:
    now = time.time()
    with _lock:
        run = _runs.get((owner, thread))
        if run is not None and not _alive(run, now):
            del _runs[(owner, thread)]
            return None
        return run


def status(owner: str, run_id: str) -> str:
    """running | done | failed | unknown (never seen here: lost in a restart, or someone else's)."""
    now = time.time()
    with _lock:
        for run in _runs.values():
            if run.id == run_id and run.owner == owner and _alive(run, now):
                return "running"
        found = _outcomes.get(run_id)
        return found[1] if found and found[0] == owner else "unknown"


def threads(owner: str) -> list[str]:
    now = time.time()
    with _lock:
        return [t for (o, t), r in _runs.items() if o == owner and _alive(r, now)]
