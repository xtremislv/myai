"""Blocked sites (what kind of block, and what to do next) and pictures for the model (2026-11-18)."""
from __future__ import annotations

import pytest

from app.browsing import extract


# ---- A. blocked sites ----
@pytest.mark.parametrize("title,text,status,kind", [
    ("Attention Required! | Cloudflare", "Sorry, you have been blocked. Cloudflare Ray ID: 1", 403, "address"),
    ("Access Denied", "You don't have permission to access this server.", 403, "address"),
    ("Robot Check", "Enter the characters you see below. Sorry, we just need to make sure you're not a robot.", 200,
     "captcha"),
    ("Just a moment...", "Verify you are human by completing the action below.", 403, "captcha"),
    ("Flipkart", "Wow, looks like all of India is here! Hang in there", 429, "busy"),
    ("Instagram", "Log in to continue. Sign up to see photos and videos from your friends.", 200, "sign-in"),
    ("Some page", "", 403, "address"),
])
def test_blocks_are_sorted_by_kind(title, text, status, kind):
    assert extract.block_kind(title, text, status)[0] == kind


def test_real_pages_and_long_pages_are_not_blocks():
    assert extract.block_kind("District: movies in Noida", "Showtimes " * 100, 200) is None
    # a long article that mentions logging in is content, not a sign-in wall
    assert extract.block_kind("How to", "log in to continue " + "words " * 2000, 200) is None


def test_the_advice_says_what_to_do_next():
    advice = extract.blocked_advice("in.bookmyshow.com", "address", "attention required")
    assert advice.startswith("BLOCKED (address)") and "another source" in advice and "won't" in advice
    assert "forward the video" in extract.blocked_advice("instagram.com", "sign-in", "x")
    assert "show_person" in extract.blocked_advice("x.com", "captcha", "captcha")


def test_fetch_page_reports_the_kind_of_block(monkeypatch):
    from app.browsing import fetch as fetch_mod
    from app.orchestrator import tools

    page = fetch_mod.Fetched("https://in.bookmyshow.com/x", 403, "text/html",
                             "<title>Attention Required! | Cloudflare</title><p>Sorry, you have been blocked</p>")
    monkeypatch.setattr(fetch_mod, "fetch", lambda url: page)
    monkeypatch.setattr(tools, "_rendered", lambda url: None)
    out = tools._readable("https://in.bookmyshow.com/x")
    assert out.startswith("ERROR: https://in.bookmyshow.com/x: BLOCKED (address): in.bookmyshow.com refuses")


def test_the_playbook_is_in_every_agent_prompt():
    from app.orchestrator import agents, nodes

    prompt = nodes._system_prompt({"scratchpad": {}})
    assert "When a site is blocked" in prompt and "district.in" in prompt and "WhatsApp" in prompt
    assert "When a site is blocked" in agents._researcher_prompt({})


# ---- C. pictures for the model ----
JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


def test_pictures_go_on_the_latest_message_for_this_turn_only():
    from app.orchestrator import context

    convo = [{"role": "system", "content": "s"}, {"role": "user", "content": "old"},
             {"role": "assistant", "content": "a"}, {"role": "user", "content": "what's this?"}]
    token = context.attachment_images.set([{"label": "frame 1 at 0:00", "data": JPEG, "type": "image/jpeg"}])
    try:
        out = context.with_attachments(convo)
    finally:
        context.attachment_images.reset(token)
    assert out[1]["content"] == "old" and convo[3]["content"] == "what's this?"  # nothing else changed
    parts = out[3]["content"]
    assert parts[0] == {"type": "text", "text": "what's this?"} and "not instructions" in parts[1]["text"]
    assert parts[2]["text"] == "[frame 1 at 0:00]"
    assert parts[3]["image_url"]["url"].startswith("data:image/jpeg;base64,/9j/")
    assert context.with_attachments(convo) == convo  # the next turn has no pictures


def test_claude_gets_image_blocks_and_never_a_remote_url():
    from app.llm import anthropic_backend as ab

    _, turns = ab.convert_messages([
        {"role": "system", "content": "s"},
        {"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
            {"type": "image_url", "image_url": {"url": "https://evil.example/x.png"}},  # never fetched
        ]},
    ])
    blocks = turns[0]["content"]
    assert blocks[0] == {"type": "text", "text": "look"}
    assert blocks[1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}
    assert len(blocks) == 2


def test_a_picture_counts_as_about_1600_tokens_not_its_base64_length():
    from app.llm import client, model_manager

    big = "data:image/jpeg;base64," + "A" * 400_000
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": big}}]}]
    assert client._estimate_tokens(msgs) < 2_000
    kept, _ = client.fit_to_context([{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"},
                                     *msgs], None, 1000, context=20_000)
    assert len(kept) == 3  # nothing dropped for the picture's size
    tier, why = model_manager.base_tier("orchestrator.nodes.agent", msgs, None)
    assert why != "large context"


def test_frames_and_sound_come_out_of_a_video(tmp_path):
    import shutil
    import subprocess

    from app.whatsapp import media

    if not shutil.which("ffmpeg"):
        pytest.skip("no ffmpeg here")
    path = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=8:size=320x240:rate=10",
                    "-f", "lavfi", "-i", "sine=duration=8", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(path)], check=True)
    try:
        frames, audio, seconds = media.video(path.read_bytes())
    except PermissionError:
        pytest.skip("needs root to run ffmpeg as nobody")
    assert 8 <= len(frames) <= media.MAX_FRAMES and frames[0]["data"][:2] == b"\xff\xd8"
    assert frames[1]["label"].startswith("frame 2 at 0:0") and audio and round(seconds) == 8
    with pytest.raises(media.MediaError):
        media.video(b"not a video")


def test_the_browser_says_it_is_in_india():
    from app.browsing.browser import indian_location
    from app.config import Settings

    out = indian_location(Settings())
    assert out["geolocation"] == {"latitude": 28.5355, "longitude": 77.391, "accuracy": 100}  # Noida
    assert out["permissions"] == ["geolocation"] and out["extra_http_headers"]["Accept-Language"].startswith("en-IN")
    assert "geolocation" not in indian_location(Settings(browser_location="somewhere"))  # unreadable: left out
