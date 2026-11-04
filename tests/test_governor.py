"""Background model calls speed up when nobody is chatting and give way when someone is."""
from __future__ import annotations

import threading
import time

from app.llm import governor as gov_mod
from app.llm.governor import (
    BUSY_SLOTS,
    IDLE_SLOTS,
    QUIET_SECONDS,
    RAMP_SLOTS,
    STARVE_SECONDS,
    STARVED_SLOTS,
    Governor,
)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_idle_is_full_speed_live_is_slow_and_it_ramps_back():
    clock = Clock()
    g = Governor(clock=clock)
    assert g.allowed() == IDLE_SLOTS
    with g.live():
        assert g.allowed() == BUSY_SLOTS
    assert g.allowed() == RAMP_SLOTS            # just finished: maybe more is coming
    clock.t += QUIET_SECONDS + 1
    assert g.allowed() == IDLE_SLOTS and g.mode() == "full speed"


def test_other_users_of_the_model_server_count_as_live(monkeypatch):
    clock = Clock()
    g = Governor(metrics_url="http://x/metrics", clock=clock)
    clock.t += QUIET_SECONDS + 1
    text = ('vllm:num_requests_running{engine="0"} 2.0\nvllm:num_requests_waiting{engine="0"} 0.0\n')

    class Resp:
        def __init__(self, body): self.body = body
        def read(self): return self.body.encode()
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(gov_mod.urllib.request, "urlopen", lambda url, timeout: Resp(text))
    assert g.allowed() == IDLE_SLOTS           # one reading could be lag from our own calls...
    clock.t += 2
    assert g.allowed() == BUSY_SLOTS           # ...two in a row is real: e.g. the scheduler's mail digest
    g._running = [object(), object()]            # but if those two are our own background calls
    clock.t += 2
    assert g._others_on_server() == 0


def test_background_calls_never_exceed_the_slots_and_wait_while_someone_chats():
    g = Governor()
    g._last_live = -1e9
    peak, now = [0], [0]
    lock = threading.Lock()

    def bg():
        with g.slot():
            with lock:
                now[0] += 1
                peak[0] = max(peak[0], now[0])
            time.sleep(0.05)
            with lock:
                now[0] -= 1

    threads = [threading.Thread(target=bg) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak[0] == IDLE_SLOTS

    peak[0] = 0
    with g.live():
        threads = [threading.Thread(target=bg) for _ in range(6)]
        [t.start() for t in threads]
        time.sleep(0.4)
        assert peak[0] == BUSY_SLOTS == 0        # none at all while someone is chatting
    [t.join() for t in threads]                  # and they all run once the chat ends


def test_background_is_not_starved_by_a_long_stretch_of_live_work():
    clock = Clock()
    g = Governor(clock=clock)
    with g.live():
        assert g.allowed() == BUSY_SLOTS
        clock.t += STARVE_SECONDS + 1
        assert g.allowed() == STARVED_SLOTS == 1


def test_a_background_jobs_calls_are_marked_background(monkeypatch):
    from app import jobs
    from app.llm import client

    seen = []
    monkeypatch.setattr(client, "_chat", lambda *a, **k: seen.append(gov_mod.background.get()) or "ok")
    monkeypatch.setattr(jobs, "_notify", lambda *a: None)
    jobs._jobs.clear()
    client.chat([{"role": "user", "content": "hi"}])
    job = jobs.start("u9", "t", lambda progress: client.chat([{"role": "user", "content": "x"}]))
    for _ in range(100):
        if job.status != "running":
            break
        time.sleep(0.02)
    assert seen == [False, True]


def test_when_someone_starts_chatting_the_newest_background_calls_stop_and_rerun():
    g = Governor()
    g._last_live = -1e9
    finished = []

    def bg(i):
        def call(should_yield):
            for _ in range(40):          # a long call, checking as it streams
                if should_yield():
                    from app.llm.governor import Paused
                    raise Paused
                time.sleep(0.01)
            return i
        finished.append(g.run(call))

    threads = [threading.Thread(target=bg, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    time.sleep(0.05)
    with g.live():
        time.sleep(0.1)
        assert g._bg <= BUSY_SLOTS       # they all stopped to make room
    [t.join() for t in threads]
    assert sorted(finished) == [0, 1, 2, 3] and g.paused >= 3   # and all still finished
