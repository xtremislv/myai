"""How many background model calls may run at once, decided from live load.

Background work (a 1,000-email sheet) shares one GPU with people chatting. Every background call
waits here for a slot; the number of slots follows the load, re-checked every half second:

  live work running   BUSY_SLOTS   0: chats and work requests get the GPU to themselves
                                   (1 after STARVE_SECONDS of unbroken live work, so it never stalls)
  live work just done RAMP_SLOTS   for QUIET_SECONDS, in case the person is still typing
  nothing else        IDLE_SLOTS   full speed

"Live work" is any model call that isn't background: counted in this process, and read from the
model server's own request counts (minus our background calls), so the scheduler's digests and
anything else using vLLM count too. A running background call streams, and stops part-way when live work needs the room (newest
first); it reruns later, cheaply, because the model server caches the prompt it already read.
Measured on the Quadro RTX 8000 (2026-11-04): throughput stops rising past ~6-8 calls at once,
and every extra call slows each live chat's words.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

IDLE_SLOTS = 8
RAMP_SLOTS = 3
BUSY_SLOTS = 0           # background waits completely while people are chatting...
STARVED_SLOTS = 1        # ...unless live work has gone on without a break for STARVE_SECONDS
STARVE_SECONDS = 60
QUIET_SECONDS = 20
CHECK_SECONDS = 0.5
METRICS_TTL = 1.0

# Set in a background job's thread (and copied into its worker threads): its calls are background.
background: ContextVar[bool] = ContextVar("background", default=False)


class Paused(Exception):
    """A background call stopped part-way to make room for live work; it is run again later
    (the model server's prefix cache keeps the prompt, so little is redone)."""


class Ticket:
    def __init__(self, owner: "Governor") -> None:
        self._owner = owner

    def should_yield(self) -> bool:
        """True when live work needs the room: the newest running background calls beyond the
        allowed number stop first, so the oldest (closest to done) finish."""
        return self._owner._rank(self) >= self._owner.allowed()

_METRIC = re.compile(r"^vllm:num_requests_(running|waiting)\{[^}]*\}\s+([0-9.eE+-]+)", re.M)


class Governor:
    def __init__(self, metrics_url: str | None = None, clock=time.monotonic) -> None:
        self._cond = threading.Condition()
        self._clock = clock
        self._running: list[Ticket] = []  # background calls in flight (this process), oldest first
        self.paused = 0              # background calls stopped to make room, since start
        self._fg = 0                 # live calls in flight (this process)
        self._last_live = -1e9       # when live work was last seen
        self._metrics_url = metrics_url
        self._server_busy = (-1e9, 0)  # (checked at, other requests on the model server)
        self._last_extra = 0         # the previous reading: outside load must show up twice in a row
        self._live_since: float | None = None  # start of the current unbroken stretch of live work

    # ---- live calls report themselves ----
    @contextmanager
    def live(self) -> Iterator[None]:
        with self._cond:
            self._fg += 1
            self._last_live = self._clock()
        try:
            yield
        finally:
            with self._cond:
                self._fg -= 1
                self._last_live = self._clock()
                self._cond.notify_all()

    # ---- background calls wait for a slot ----
    @property
    def _bg(self) -> int:
        return len(self._running)

    def _rank(self, ticket: Ticket) -> int:
        with self._cond:
            return self._running.index(ticket) if ticket in self._running else 0

    @contextmanager
    def slot(self) -> Iterator[Ticket]:
        ticket = Ticket(self)
        with self._cond:
            while self._bg >= self.allowed():
                self._cond.wait(CHECK_SECONDS)
            self._running.append(ticket)
        try:
            yield ticket
        finally:
            with self._cond:
                self._running.remove(ticket)
                self._cond.notify_all()

    def run(self, call):
        """call(should_yield) as background work: waits for a slot, and if it is stopped to make
        room for live work, waits again and reruns it."""
        while True:
            with self.slot() as ticket:
                try:
                    return call(ticket.should_yield)
                except Paused:
                    with self._cond:
                        self.paused += 1
            time.sleep(CHECK_SECONDS)  # let the live call take the room before queueing again

    def _others_on_server(self) -> int:
        """Requests on the model server that aren't our background calls (0 if unknown)."""
        if not self._metrics_url:
            return 0
        checked, others = self._server_busy
        now = self._clock()
        if now - checked >= METRICS_TTL:
            mine = self._bg + self._fg
            try:
                with urllib.request.urlopen(self._metrics_url, timeout=0.5) as resp:  # noqa: S310 - local URL
                    text = resp.read().decode()
                extra = max(0, int(sum(float(v) for _, v in _METRIC.findall(text))) - mine)
            except Exception:  # noqa: BLE001 - no metrics: fall back to this process's own count
                extra = 0
            # The server's count lags a moment behind our own calls ending: only believe outside
            # load that two readings in a row agree on.
            others = min(extra, self._last_extra)
            self._last_extra = extra
            self._server_busy = (now, others)
        return others

    def allowed(self) -> int:
        live = self._fg + self._others_on_server()
        now = self._clock()
        if live > 0:
            self._last_live = now
            if self._live_since is None:
                self._live_since = now
            return STARVED_SLOTS if now - self._live_since >= STARVE_SECONDS else BUSY_SLOTS
        self._live_since = None
        if self._clock() - self._last_live < QUIET_SECONDS:
            return RAMP_SLOTS
        return IDLE_SLOTS

    def mode(self) -> str:
        n = self.allowed()
        if n == IDLE_SLOTS:
            return "full speed"
        if n <= STARVED_SLOTS:
            return "paused for live chats" if n == 0 else "slowed for live chats"
        return "picking up speed"

    def snapshot(self) -> dict:
        return {"background": self._bg, "live": self._fg, "slots": self.allowed(), "paused": self.paused}


def _default() -> Governor:
    from app.config import get_settings

    base = get_settings().llm_base_url.rstrip("/")
    return Governor(re.sub(r"/v1$", "", base) + "/metrics")


governor = _default()
