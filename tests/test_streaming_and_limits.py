"""Streaming replies, GPU concurrency limits, and tool arguments in prompts."""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.security import issue_token
from app.db.session import Base, ChatThread, Department, get_session
from app.departments.registry import seed_departments
from app.llm import client as llm
from app.main import app
from app.orchestrator import execution, tools
from app.orchestrator.graph import build_graph
from app.orchestrator.multi_graph import build_team_graph
from app.orchestrator.nodes import _system_prompt
from app.request_manager import concurrency, guards, quota, router
from app.request_manager.concurrency import ConcurrencyLimiter, InMemorySlots, RedisSlots
from app.request_manager.router import RMRequest, handle_request

KEY = os.urandom(32)
ALICE = {"authorization": f"Bearer {issue_token('alice', 'alice', ['logistics'], chat_key=KEY)}"}
WORDS = ["Hello", " there", ", friend"]


def _chunk(text=None, usage=None):
    choices = [SimpleNamespace(delta=SimpleNamespace(content=text))] if text is not None else []
    return SimpleNamespace(choices=choices, usage=usage)


class FakeModel:
    """Stands in for vLLM below llm.chat, so the real client code runs."""

    def __init__(self):
        self.calls = []
        self.fail = False

    def create(self, **params):
        self.calls.append(params)
        if self.fail:
            raise ConnectionError("vLLM is down: SECRET PROMPT TEXT")
        if params.get("stream"):
            return iter(
                [_chunk(w) for w in WORDS]
                + [_chunk(usage=SimpleNamespace(prompt_tokens=3, completion_tokens=3))]
            )
        message = SimpleNamespace(content="".join(WORDS))
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(message=message)])


@pytest.fixture
def api(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        seed_departments(s)

    def _get_session():
        with Session() as s:
            yield s

    saver = MemorySaver()
    plain = saver.serde

    @contextmanager
    def _cm(mode, serde=None):
        saver.serde = serde or plain
        builder = build_team_graph() if mode == "team" else build_graph()
        yield builder.compile(checkpointer=saver)

    monkeypatch.setattr(execution, "_graph_cm", _cm)
    limiter = quota.InMemoryQuota()
    monkeypatch.setattr(router, "default_quota", lambda: limiter)
    model = FakeModel()
    monkeypatch.setattr(llm, "_client", SimpleNamespace(chat=SimpleNamespace(completions=model)))
    app.dependency_overrides[get_session] = _get_session
    yield SimpleNamespace(client=TestClient(app), Session=Session, model=model)
    app.dependency_overrides.clear()


def _events(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return [json.loads(line[6:]) for line in response.text.split("\n\n") if line.startswith("data: ")]


def _stream(api, message="hello", thread="t", **extra):
    return api.client.post(
        "/v1/chat/stream", headers=ALICE, json={"message": message, "thread_id": thread, **extra}
    )


def _sorted_as(monkeypatch, **verdict):
    monkeypatch.setattr(
        guards, "triage_verdict",
        lambda *a: {"safety": "none", "route": "private", "action": "none", "steps": "one", **verdict},
    )


# ---- streaming ----
def test_private_reply_streams_word_by_word_then_finishes(api):
    events = _events(_stream(api))
    assert [e["text"] for e in events if e["type"] == "delta"] == WORDS
    done = events[-1]
    assert done["type"] == "done"
    assert (done["reply"], done["route"], done["outcome"]) == ("Hello there, friend", "private", "ok")


def test_streamed_reply_is_saved_and_listed(api):
    _events(_stream(api, "REMEMBER ME"))
    history = api.client.get("/v1/chat/threads/t/messages", headers=ALICE).json()
    assert [m["content"] for m in history] == ["REMEMBER ME", "Hello there, friend"]
    assert api.client.get("/v1/chat/threads", headers=ALICE).json()[0]["thread_id"] == "t"


def test_stream_uses_the_same_encryption_as_plain_chat(api):
    _events(_stream(api, "SENSITIVE TITLE"))
    with api.Session() as s:
        [row] = s.query(ChatThread).all()
    assert "SENSITIVE" not in row.encrypted_title


def test_team_mode_streams_only_the_final_answer(api, monkeypatch):
    _sorted_as(monkeypatch, steps="many")
    events = _events(_stream(api))
    assert events[-1]["mode"] == "team"
    streamed = [call for call in api.model.calls if call.get("stream")]
    assert len(streamed) == 1 and "WRITER" in streamed[0]["messages"][0]["content"]
    assert [e["text"] for e in events if e["type"] == "delta"] == WORDS


def test_work_arrives_whole_with_its_department(api, monkeypatch):
    _sorted_as(monkeypatch, route="work", department="logistics")
    events = _events(_stream(api))
    assert [e["type"] for e in events] == ["done"]
    assert (events[0]["route"], events[0]["department"]) == ("work", "logistics")
    assert not any(call.get("stream") for call in api.model.calls), "tool calls must never reach the user"


def test_flagged_message_streams_nothing_and_is_not_kept(api, monkeypatch):
    _sorted_as(monkeypatch, safety="illegal")
    events = _events(_stream(api, "BAD"))
    assert [e["type"] for e in events] == ["done"] and events[0]["outcome"] == "flagged_nsfw"
    assert api.model.calls == []
    assert api.client.get("/v1/chat/threads/t/messages", headers=ALICE).json() == []


def test_failure_gives_a_plain_error_without_details_or_logged_content(api, caplog):
    api.model.fail = True
    with caplog.at_level("DEBUG"):
        events = _events(_stream(api, "MY PRIVATE MESSAGE"))
    assert events[-1]["type"] == "error"
    assert "SECRET" not in events[-1]["detail"]
    assert "MY PRIVATE MESSAGE" not in caplog.text and "SECRET PROMPT TEXT" not in caplog.text


def test_stream_needs_a_chat_key(api):
    token = issue_token("nokey", "nokey", [])
    response = api.client.post(
        "/v1/chat/stream", headers={"authorization": f"Bearer {token}"}, json={"message": "x", "thread_id": "t"}
    )
    assert response.status_code == 403


def test_plain_chat_endpoint_still_works_without_streaming(api):
    body = api.client.post("/v1/chat", headers=ALICE, json={"message": "hi", "thread_id": "t"}).json()
    assert body["reply"] == "Hello there, friend"
    assert not any(call.get("stream") for call in api.model.calls)


def test_streaming_records_token_usage(api):
    before = llm.llm_tokens_total.labels(model=llm._settings.llm_model, kind="prompt")._value.get()
    _events(_stream(api))
    after = llm.llm_tokens_total.labels(model=llm._settings.llm_model, kind="prompt")._value.get()
    assert after == before + 3


# ---- private chat limits ----
def test_one_person_cannot_take_every_slot(api, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "private_max_per_user", 0)
    response = api.client.post("/v1/chat", headers=ALICE, json={"message": "hi", "thread_id": "t"})
    assert response.status_code == 429 and "busy" in response.json()["detail"]
    assert _events(_stream(api))[-1]["type"] == "error"
    assert api.model.calls == []


def test_shared_pool_limit_applies_too(api, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "private_max_total", 0)
    assert api.client.post("/v1/chat", headers=ALICE, json={"message": "hi", "thread_id": "t"}).status_code == 429


def test_slots_are_given_back_after_a_reply(api, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "private_max_per_user", 1)
    for _ in range(3):
        assert _events(_stream(api))[-1]["type"] == "done"


# ---- department limits ----
def test_department_over_its_limit_is_turned_away_without_running(api):
    with api.Session() as s:
        s.get(Department, "logistics").max_concurrency = 1
        s.commit()
        limiter = ConcurrencyLimiter(InMemorySlots())
        ran = []

        with limiter.slot("dept:logistics", 1):  # someone else is running
            result = handle_request(
                s, SimpleNamespace(id="lee", roles=("logistics",)), RMRequest("work", "t"),
                quota=quota.InMemoryQuota(), concurrency=limiter, runner=lambda *a: ran.append(1),
            )

        assert result.outcome == "denied_busy" and ran == []


def test_slot_is_free_again_even_if_the_run_fails(api):
    limiter = ConcurrencyLimiter(InMemorySlots())

    def _boom(*a):
        raise RuntimeError("failed with detail")

    with api.Session() as s:
        s.get(Department, "logistics").max_concurrency = 1
        s.commit()
        subject = SimpleNamespace(id="lee", roles=("logistics",))
        for _ in range(2):
            result = handle_request(
                s, subject, RMRequest("work", "t"), quota=quota.InMemoryQuota(), concurrency=limiter, runner=_boom
            )
            assert result.outcome == "error"
            assert "detail" not in result.reason


def test_busy_department_returns_429_on_the_api(api, monkeypatch):
    class Full:
        @contextmanager
        def slot(self, key, limit):
            yield False

    monkeypatch.setattr(concurrency, "default_concurrency", lambda: Full())
    response = api.client.post("/v1/request", headers=ALICE, json={"message": "work", "thread_id": "t"})
    assert response.status_code == 429


def test_busy_department_is_explained_in_the_chat(api, monkeypatch):
    class Full:
        @contextmanager
        def slot(self, key, limit):
            yield False

    monkeypatch.setattr(concurrency, "default_concurrency", lambda: Full())
    _sorted_as(monkeypatch, route="work", department="logistics")
    done = _events(_stream(api))[-1]
    assert done["outcome"] == "denied_busy" and "Logistics is busy" in done["reply"]


# ---- the limiter itself ----
class FakeRedis:
    def __init__(self):
        self.values = {}

    def incr(self, name):
        self.values[name] = self.values.get(name, 0) + 1
        return self.values[name]

    def decr(self, name):
        self.values[name] = self.values.get(name, 0) - 1
        return self.values[name]

    def expire(self, name, seconds):
        self.ttl = seconds

    def delete(self, name):
        self.values.pop(name, None)


@pytest.mark.parametrize("make", [InMemorySlots, lambda: RedisSlots(FakeRedis())])
def test_limit_is_enforced_and_released(make):
    limiter = ConcurrencyLimiter(make())
    with limiter.slot("k", 1) as first:
        assert first
        with limiter.slot("k", 1) as second:
            assert not second
    with limiter.slot("k", 1) as again:
        assert again


def test_refused_slot_does_not_use_up_capacity():
    redis = FakeRedis()
    limiter = ConcurrencyLimiter(RedisSlots(redis))
    with limiter.slot("k", 1):
        for _ in range(5):
            with limiter.slot("k", 1) as free:
                assert not free
    assert redis.values == {}


def test_redis_counts_expire_in_case_a_process_crashes():
    redis = FakeRedis()
    RedisSlots(redis).acquire("k", 1)
    assert redis.ttl == concurrency.SLOT_TTL_SECONDS


class DownRedis:
    def acquire(self, key, limit):
        raise ConnectionError("redis is down")

    def release(self, key):
        raise AssertionError("must release to the backend that gave the slot")


def test_redis_outage_still_limits():
    limiter = ConcurrencyLimiter(DownRedis(), InMemorySlots())
    with limiter.slot("k", 1) as first:
        assert first
        with limiter.slot("k", 1) as second:
            assert not second


def test_redis_going_down_mid_run_does_not_break_the_request():
    class Dies(RedisSlots):
        def release(self, key):
            raise ConnectionError("redis went down")

    with ConcurrencyLimiter(Dies(FakeRedis())).slot("k", 1) as free:
        assert free


def test_in_memory_limit_holds_under_threads():
    slots = InMemorySlots()
    wins = []
    barrier = threading.Barrier(8)

    def _try():
        barrier.wait()
        if slots.acquire("k", 3):
            wins.append(1)

    workers = [threading.Thread(target=_try) for _ in range(8)]
    [w.start() for w in workers]
    [w.join() for w in workers]
    assert len(wins) == 3


# ---- tool arguments ----
def test_signatures_show_arguments_and_which_are_optional():
    signatures = tools.tool_signatures("logistics")
    assert "track_shipment(shipment_id)" in signatures
    assert "inventory_lookup(sku?)" in signatures
    assert "send_notification(to, text)" in signatures


def test_signatures_never_expose_the_internal_state_argument():
    for department in ("account", "hr", "logistics", "researcher", "superuser", "admin"):
        assert not any("state" in s for s in tools.tool_signatures(department))


def test_agent_prompt_tells_the_model_the_arguments():
    prompt = _system_prompt({"scratchpad": {"department": "account"}})
    assert "initiate_payment(payee, amount, currency?, reference?)" in prompt


def test_team_researcher_prompt_tells_the_model_the_arguments():
    from app.orchestrator.agents import _researcher_prompt

    assert "read_document(key)" in _researcher_prompt({"scratchpad": {"department": "researcher"}})
