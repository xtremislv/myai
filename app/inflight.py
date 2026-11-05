"""Chat requests still being worked on, so a person who left the chat (another page, another tab, a
reload, a dropped connection) sees on return that their request is still running, and its steps.

In memory only: the question and the step lines live here while the request runs and are dropped
when it ends (the reply itself is saved in the conversation as usual). Nothing is logged.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

MAX_STEPS = 40
FORGET_AFTER = 3600  # a run that never reported its end (a crash) is forgotten after an hour


@dataclass
class Running:
    owner: str
    thread: str
    message: str
    started: float = field(default_factory=time.time)
    steps: list[str] = field(default_factory=list)
    done: bool = False


_runs: dict[tuple[str, str], Running] = {}
_lock = threading.Lock()


def start(owner: str, thread: str, message: str) -> Running:
    run = Running(owner, thread, message)
    with _lock:
        _runs[(owner, thread)] = run
    return run


def step(run: Running, text: str) -> None:
    with _lock:
        if not run.done:
            run.steps = (run.steps + [text])[-MAX_STEPS:]


def finish(run: Running) -> None:
    with _lock:
        run.done = True
        if _runs.get((run.owner, run.thread)) is run:
            del _runs[(run.owner, run.thread)]


def get(owner: str, thread: str) -> Running | None:
    with _lock:
        run = _runs.get((owner, thread))
        if run is not None and time.time() - run.started > FORGET_AFTER:
            del _runs[(owner, thread)]
            return None
        return run


def threads(owner: str) -> list[str]:
    now = time.time()
    with _lock:
        return [t for (o, t), r in _runs.items() if o == owner and now - r.started <= FORGET_AFTER]
