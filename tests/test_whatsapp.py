"""The WhatsApp bot (app/whatsapp/): linking, the allowlist, replies and the result page, tools on the
WhatsApp channel, notifications in and outside the 24-hour window, approvals by button, and the webhook.
Meta's API is a MockTransport: nothing leaves the test."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base, User, WhatsAppLink
from app.vault.aesgcm_vault import AESGCMVault
from app.whatsapp import bot, cloud, links
from app.whatsapp import format as fmt

PHONE = "919812345678"
MEDIA: dict = {"data": b"", "mime": "text/plain"}
BOT = "919900000000"
CFG = {"token": "t", "phone_id": "PID", "app_secret": "shh", "verify_token": "vt", "display_number": BOT,
       "template": "twai_notification"}


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(bot, "SessionLocal", factory)
    from app.api import routes_whatsapp

    monkeypatch.setattr(routes_whatsapp, "SessionLocal", factory)
    from contextlib import contextmanager

    from langgraph.checkpoint.memory import MemorySaver

    from app.orchestrator import execution

    saver = MemorySaver()
    plain = saver.serde

    @contextmanager
    def _cm(mode, serde=None):  # conversations in memory, not Postgres
        saver.serde = serde or plain
        yield execution.builder(mode).compile(checkpointer=saver)

    monkeypatch.setattr(execution, "_graph_cm", _cm)
    with factory() as s:
        s.add(User(id="u1", username="lovy", password_hash="x", roles=["researcher"]))
        s.add(User(id="adm", username="admin", password_hash="x", roles=["admin"]))
        s.commit()
        AESGCMVault(s).put(cloud.SECRET, json.dumps(CFG))
    return factory


@pytest.fixture
def sent(monkeypatch):
    """What the bot sent to Meta, in order."""
    out: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":  # media: the id's lookup, then the file itself
            if request.url.host == "files.example":
                return httpx.Response(200, content=MEDIA["data"])
            return httpx.Response(200, json={"url": "https://files.example/m", "mime_type": MEDIA["mime"],
                                              "file_size": len(MEDIA["data"])})
        payload = json.loads(request.content)
        if payload.get("typing_indicator") and MEDIA.get("no_typing"):
            return httpx.Response(400, json={"error": {"message": "unknown field", "code": 100}})
        out.append(payload)
        return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})

    monkeypatch.setattr(cloud, "transport", httpx.MockTransport(handler))
    monkeypatch.setattr(bot, "_submit", lambda fn, *args: fn(*args))  # run "background" work inline
    monkeypatch.setattr(bot, "_once", _fresh_once())
    return out


def _fresh_once():
    seen: set[str] = set()

    def once(key: str, seconds: int) -> bool:
        if key in seen:
            return False
        seen.add(key)
        return True

    return once


def _texts(sent) -> list[str]:
    return [m["text"]["body"] for m in sent if m.get("type") == "text"]


def _msg(text: str, frm: str = PHONE, mid: str = "wamid.1") -> dict:
    return {"from": frm, "id": mid, "type": "text", "text": {"body": text}, "phone_id": "PID"}


def _link(db, user_id: str = "u1", phone: str = PHONE) -> None:
    with db() as s:
        code = links.start(s, user_id, "+" + phone)
        assert links.confirm(s, phone, code) is not None


# ---- linking ----
def test_numbers_are_normalised_with_the_country_code():
    assert links.normalize_phone("+91 98123 45678") == PHONE
    assert links.normalize_phone("98123 45678") == PHONE
    assert links.normalize_phone("098123 45678") == PHONE
    assert links.normalize_phone("0044 7700 900123") == "447700900123"
    for bad in ("12345", "+0 123 456 7890", ""):
        with pytest.raises(links.LinkError):
            links.normalize_phone(bad)


def test_a_number_is_linked_only_by_its_own_code_before_it_expires(db):
    with db() as s:
        code = links.start(s, "u1", "98123 45678")
        assert links.confirm(s, "919800000000", code) is None          # another number
        assert links.confirm(s, PHONE, "000000" if code != "000000" else "111111") is None
        row = links.get(s, "u1")
        row.code_expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
        s.commit()
        assert links.confirm(s, PHONE, code) is None                   # expired
        code = links.start(s, "u1", PHONE)
        assert links.confirm(s, PHONE, code).phone == PHONE
        assert links.get(s, "u1").code_hash is None
        with pytest.raises(links.LinkError):                           # one account per number
            links.start(s, "adm", PHONE)


def test_strangers_are_told_once_and_a_link_code_links_them(db, sent):
    bot.handle_message(_msg("hello", mid="a"))
    bot.handle_message(_msg("hello again", mid="b"))
    assert _texts(sent) == [bot.NOT_LINKED]
    with db() as s:
        code = links.start(s, "u1", PHONE)
    bot.handle_message(_msg(f"link {code}", mid="c"))
    assert _texts(sent)[-1].startswith("Linked to your TW-ai account (lovy)")
    with db() as s:
        assert links.by_phone(s, PHONE).user_id == "u1"


def test_disabled_accounts_and_other_bot_numbers_get_nothing(db, sent):
    _link(db)
    with db() as s:
        s.get(User, "u1").disabled = True
        s.commit()
    bot.handle_message(_msg("hi"))
    bot.handle_message({**_msg("hi", mid="x"), "phone_id": "OTHER"})
    assert sent == []


# ---- answering ----
def test_a_short_answer_comes_back_as_whatsapp_text(db, sent):
    _link(db)
    seen = {}

    def runner(subject, text, thread, mode, department, *, chat_key, attachments=None):
        from app.orchestrator.context import channel, user_key

        seen.update(subject=subject.id, text=text, thread=thread, channel=channel.get(), key=chat_key,
                    tool_key=user_key.get())
        return "## Done\nThe **best** pick is [boAt](https://boat-lifestyle.com/x)."

    bot.handle_message(_msg("best earbuds under 2000?"), runner=runner)
    assert seen["subject"] == "u1" and seen["thread"] == "whatsapp-1" and seen["channel"] == "whatsapp"
    with db() as s:
        assert seen["key"] == links.chat_key(s, "u1") and seen["tool_key"] is None
    reads = [m for m in sent if m.get("status") == "read"]
    assert reads and reads[0]["message_id"] == "wamid.1" and reads[0]["typing_indicator"] == {"type": "text"}
    assert _texts(sent)[-1] == "*Done*\nThe *best* pick is boAt: https://boat-lifestyle.com/x."


def test_long_answers_and_tables_go_to_the_result_page(db, sent, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "public_app_url", "https://tw-ai.in/")
    _link(db)
    answer = "Here are the picks.\n\n| Name | Price |\n|---|---|\n| A | 100 |\n| B | 200 |"
    bot.handle_message(_msg("compare"), runner=lambda *a, **k: answer)
    text = _texts(sent)[-1]
    assert text.startswith("Here are the picks.") and "| A |" not in text
    url = text.rsplit(" ", 1)[-1]
    assert url.startswith("https://tw-ai.in/r/")
    with db() as s:
        page = links.load_page(s, url.rsplit("/", 1)[-1])
        assert page["markdown"] == answer
        # The same link every time, until the person makes a new one.
        row = links.get(s, "u1")
        assert links.page_url(s, row) == url
        new = links.reset_page(s, row)
        assert new != url and links.load_page(s, url.rsplit("/", 1)[-1]) is None
        assert links.load_page(s, new.rsplit("/", 1)[-1])["markdown"] == ""


def test_old_pages_are_purged(db):
    _link(db)
    with db() as s:
        row = links.get(s, "u1")
        links.save_page(s, row, "t", "x", [])
        assert links.purge_pages(s, dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=8)) == 1
        assert links.get(s, "u1").page is None


def test_commands(db, sent):
    _link(db)
    bot.handle_message(_msg("/reset", mid="r"))
    bot.handle_message(_msg("/mute", mid="m"))
    with db() as s:
        row = links.get(s, "u1")
        assert row.thread_version == 2 and row.notify is False
    assert _texts(sent)[0] == "New conversation started."


def test_one_message_at_a_time_per_conversation(db, sent):
    from app import inflight

    _link(db)
    running = inflight.start("u1", "whatsapp-1", "first", "whatsapp")
    try:
        bot.handle_message(_msg("second"), runner=lambda *a, **k: "should not run")
    finally:
        inflight.finish(running)
    assert _texts(sent)[-1].startswith("Still working on your last message")


def test_flagged_messages_get_no_answer_text(db, sent, monkeypatch):
    from app.request_manager import guards

    _link(db)
    reported = []
    monkeypatch.setattr(guards, "report_nsfw", lambda s, uid, m: reported.append(uid))
    real = guards.triage

    def flagged(*args, **kwargs):
        verdict = real(*args, **kwargs)
        return type(verdict)(**{**verdict.__dict__, "nsfw": True, "why": "test"})

    monkeypatch.setattr(guards, "triage", flagged)
    bot.handle_message(_msg("something"), runner=lambda *a, **k: "never")
    assert reported == ["u1"] and "reported to HR and the admin" in _texts(sent)[-1]


# ---- the WhatsApp channel's tools ----
def test_whatsapp_leaves_out_the_persons_own_data_and_person_present_tools():
    from app.departments.packs import PERSON_PRESENT_TOOLS
    from app.orchestrator import tools
    from app.orchestrator.context import channel

    app_private = set(tools.tools_for(None))
    token = channel.set("whatsapp")
    try:
        private = set(tools.tools_for(None))
        research = set(tools.tools_for("researcher"))
    finally:
        channel.reset(token)
    assert {"read_file", "write_file", "read_mail", "send_mail", "fill_saved", "browser_open", "web_research"} & private == set()
    assert {"web_search", "gadget_search", "set_reminder", "make_dashboard"} <= private <= app_private
    assert not research & set(PERSON_PRESENT_TOOLS) and "web_search" in research


# ---- notifications ----
def test_notifications_inside_the_window_as_text_outside_as_the_template(db, sent):
    from app.reminders import service

    _link(db)
    with db() as s:
        service.notify(s, "u1", "reminder", "Call the courier", "Reminder set for 5 pm")
    assert _texts(sent)[-1] == "*Reminder: Call the courier*\n\nReminder set for 5 pm"
    with db() as s:
        links.get(s, "u1").last_inbound_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=30)
        s.commit()
        service.notify(s, "u1", "reminder", "Call the courier", "x")
    template = sent[-1]["template"]
    assert template["name"] == "twai_notification"
    assert template["components"][0]["parameters"][0]["text"] == "Reminder: Call the courier"


def test_muted_or_unlinked_people_get_no_whatsapp_notifications(db, sent):
    from app.reminders import service

    with db() as s:
        service.notify(s, "u1", "alert", "x")
    _link(db)
    with db() as s:
        links.get(s, "u1").notify = False
        s.commit()
        service.notify(s, "u1", "alert", "x")
    assert sent == []


def test_template_parameters_are_flattened():
    assert cloud.template_param("a\n\nb\t c     d") == "a b c d"
    assert len(cloud.template_param("x" * 500)) == cloud.TEMPLATE_PARAM_LIMIT


# ---- approvals ----
def test_admins_get_approve_buttons_and_a_button_decides(db, sent, monkeypatch):
    from app.request_manager import approvals as store
    from app.request_manager import router

    _link(db, "adm", "919811111111")
    with db() as s:
        row = store.create(s, department="researcher", subject_id="u1", message="buy the sample", thread_id="t",
                           mode="single", tool="manage_order", tool_args={"item": "earbuds"}, checkpoint_thread="u1:t",
                           chat_thread="whatsapp-1")
    buttons = [m for m in sent if m.get("type") == "interactive"][-1]
    assert buttons["to"] == "919811111111"
    ids = [b["reply"]["id"] for b in buttons["interactive"]["action"]["buttons"]]
    assert ids == [f"ap:{row.id}:y", f"ap:{row.id}:n"]
    resolved = []
    monkeypatch.setattr(router, "resolve_approval", lambda s, a: resolved.append(a.state) or router.RMResult("ok", "researcher"))
    bot.handle_message({"from": "919811111111", "id": "b1", "type": "interactive", "phone_id": "PID",
                        "interactive": {"type": "button_reply", "button_reply": {"id": ids[0], "title": "Approve"}}})
    assert resolved == ["approved"] and _texts(sent)[-2:] == ["Approved. Running it now.", "Done."]
    bot.handle_message({"from": "919811111111", "id": "b2", "type": "interactive", "phone_id": "PID",
                        "interactive": {"type": "button_reply", "button_reply": {"id": ids[1], "title": "Reject"}}})
    assert _texts(sent)[-1] == "Already approved."


def test_only_admins_can_decide_by_button(db, sent):
    _link(db)
    bot.handle_message({"from": PHONE, "id": "b", "type": "interactive", "phone_id": "PID",
                        "interactive": {"type": "button_reply", "button_reply": {"id": f"ap:{'a' * 32}:y", "title": "Approve"}}})
    assert _texts(sent)[-1] == "Only the admin can approve requests."


def test_work_results_reach_a_requester_who_asked_on_whatsapp(db, sent):
    _link(db)
    approval = type("A", (), {"chat_thread": "whatsapp-1", "subject_id": "u1", "state": "approved", "reason": ""})()
    with db() as s:
        bot.work_result(s, approval, "Ordered: 2 units.")
        bot.work_result(s, type("A", (), {**approval.__dict__, "chat_thread": "app-thread"})(), "not sent")
    assert _texts(sent) == ["Your request was approved.\n\nOrdered: 2 units."]


# ---- format ----
def test_markdown_becomes_whatsapp_text():
    src = "# Title\n\n* one\n* **two**\n\n---\n\n```\n**kept**\n```"
    assert fmt.to_whatsapp(src) == "*Title*\n\n- one\n- *two*\n\n```\n**kept**\n```"
    assert fmt.needs_page("x" * (fmt.PAGE_OVER + 1)) and fmt.needs_page("x", [{}]) and not fmt.needs_page("short")
    parts = fmt.chunks(("word " * 1000 + "\n\n") * 3, limit=2000)
    assert all(len(p) <= 2000 for p in parts) and len(parts) >= 7


# ---- the webhook ----
def _signed(body: bytes, secret: str = "shh") -> dict:
    return {"x-hub-signature-256": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
            "content-type": "application/json"}


def test_webhook_checks_the_verify_token_and_every_signature(db, monkeypatch):
    from app.db.session import get_session
    from app.main import app

    app.dependency_overrides[get_session] = lambda: (yield from _session(db))
    queued = []
    monkeypatch.setattr(bot, "handle_payload", lambda payload: queued.append(payload) or 1)
    try:
        client = TestClient(app)
        ok = client.get("/v1/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"})
        assert ok.status_code == 200 and ok.text == "42"
        assert client.get("/v1/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no"}).status_code == 403
        body = json.dumps({"entry": []}).encode()
        assert client.post("/v1/whatsapp/webhook", content=body, headers=_signed(body, "wrong")).status_code == 401
        assert client.post("/v1/whatsapp/webhook", content=body, headers=_signed(body)).status_code == 200
        assert queued == [{"entry": []}]
        page = client.get("/r/sometoken")
        assert page.status_code == 200 and 'data-page="result"' in page.text and "/ui/app.js?v=" in page.text
        assert page.headers["referrer-policy"] == "no-referrer" and "script-src 'self'" in page.headers["content-security-policy"]
        assert client.get("/v1/whatsapp/page/nope").status_code == 404
    finally:
        app.dependency_overrides.clear()


def _session(factory):
    with factory() as s:
        yield s


def test_incoming_skips_status_updates_and_other_fields():
    payload = {"entry": [{"changes": [
        {"field": "messages", "value": {"metadata": {"phone_number_id": "PID"},
                                        "statuses": [{"id": "s"}], "messages": [_msg("hi")]}},
        {"field": "account_update", "value": {"messages": [_msg("no")]}},
    ]}]}
    assert [m["text"]["body"] for m in cloud.incoming(payload)] == ["hi"]


def test_erasing_a_person_unlinks_their_number(db):
    from app import privacy

    _link(db)
    with db() as s:
        privacy.erase_user(s, "u1", remove_object=lambda ref: None)
        assert s.get(WhatsAppLink, "u1") is None


# ---- voice notes and documents ----
def _media_msg(kind: str, mid: str = "m1", **extra) -> dict:
    return {"from": PHONE, "id": mid, "type": kind, kind: {"id": "MEDIAID", **extra}, "phone_id": "PID"}


def test_a_voice_note_is_transcribed_and_answered(db, sent, monkeypatch):
    from app.api import routes_voice

    _link(db)
    MEDIA.update(data=b"OggS...", mime="audio/ogg; codecs=opus", no_typing=False)
    monkeypatch.setattr(routes_voice, "transcribe", lambda data, language="": {"text": " remind me at 5 "})
    seen = {}
    bot.handle_message(_media_msg("audio", voice=True), runner=lambda s, text, *a, **k: seen.update(text=text) or "Set for 5 pm.")
    assert seen["text"] == "remind me at 5"
    assert _texts(sent)[-1] == '_Heard: "remind me at 5"_\n\nSet for 5 pm.'


def test_a_document_is_kept_with_the_conversation_for_three_days(db, sent):
    _link(db)
    MEDIA.update(data=b"Invoice 42: total 1,200 INR", mime="text/plain", no_typing=False)
    bot.handle_message(_media_msg("document", filename="invoice.txt"))
    assert _texts(sent)[-1].startswith("Got invoice.txt. Ask me about it")
    seen = {}
    bot.handle_message(_msg("what is the total?", mid="q"),
                       runner=lambda *a, attachments=None, **k: seen.update(a=attachments) or "1,200 INR")
    assert "--- invoice.txt ---" in seen["a"] and "total 1,200 INR" in seen["a"]
    bot.handle_message(_msg("/reset", mid="r"))
    with db() as s:
        assert links.files_context(s, links.get(s, "u1")) is None


def test_a_document_with_a_caption_is_answered_at_once(db, sent):
    _link(db)
    MEDIA.update(data=b"Q3 sales up 12%", mime="text/plain", no_typing=False)
    seen = {}
    bot.handle_message(_media_msg("document", filename="q3.txt", caption="summarize this"),
                       runner=lambda s, text, *a, attachments=None, **k: seen.update(text=text, a=attachments) or "Up 12%.")
    assert seen["text"] == "summarize this" and "Q3 sales up 12%" in seen["a"] and _texts(sent)[-1] == "Up 12%."


def test_unreadable_or_too_large_files_are_refused(db, sent, monkeypatch):
    _link(db)
    MEDIA.update(data=b"\\x89PNG", mime="image/png", no_typing=False)
    bot.handle_message(_media_msg("document", filename="photo.png"))
    assert _texts(sent)[-1] == "I can't read photo.png: PDF, Word, Excel and text files work."
    monkeypatch.setattr(bot, "DOC_MAX", 3)
    MEDIA.update(data=b"1234", mime="text/plain")
    bot.handle_message(_media_msg("document", mid="m2", filename="big.txt"))
    assert _texts(sent)[-1].startswith("That file is too large")
    bot.handle_message({"from": PHONE, "id": "m3", "type": "image", "image": {"id": "x"}, "phone_id": "PID"})
    assert _texts(sent)[-1] == bot.ONLY_TEXT


def test_an_api_without_typing_indicators_still_gets_the_read_receipt(db, sent):
    _link(db)
    MEDIA.update(no_typing=True)
    try:
        bot.handle_message(_msg("/help"))
    finally:
        MEDIA.update(no_typing=False)
    reads = [m for m in sent if m.get("status") == "read"]
    assert reads == [{"messaging_product": "whatsapp", "status": "read", "message_id": "wamid.1"}]
