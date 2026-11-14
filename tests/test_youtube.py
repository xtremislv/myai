"""YouTube Data API wrapper (app/browsing/youtube.py) and its tools, with a fake API."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from app.browsing import youtube
from app.browsing.fetch import Fetched
from app.orchestrator import tools as tools_mod

KEY = "AIzaTESTKEY"
OURS = "UCdp6GUwjKscp5ST4M4WgIpw"


def _channel(cid, title, handle, subs, views, videos):
    return {"id": cid, "snippet": {"title": title, "customUrl": handle, "publishedAt": "2011-09-11T00:00:00Z",
                                   "country": "IN", "description": ""},
            "statistics": {"subscriberCount": str(subs), "viewCount": str(views), "videoCount": str(videos)},
            "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid[2:]}}}


def _video(vid, title, views, duration="PT10M"):
    return {"id": vid, "snippet": {"title": title, "channelTitle": "TechWiser", "publishedAt": "2026-11-01T00:00:00Z",
                                   "tags": ["gadgets"], "description": "desc"},
            "statistics": {"viewCount": str(views), "likeCount": "10", "commentCount": "2"},
            "contentDetails": {"duration": duration}}


@pytest.fixture
def api(monkeypatch):
    calls = []

    def fake_fetch(url, headers=None):
        parts = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert q.pop("key") == KEY
        path = parts.path.rsplit("/", 1)[-1]
        calls.append((path, q))
        if path == "channels" and q.get("forHandle") == "@other":
            body = {"items": [{"id": "UC" + "o" * 22}]}
        elif path == "channels":
            body = {"items": [_channel(c, "TechWiser" if c == OURS else "Other", "@techwiser" if c == OURS else "@other",
                                       2_590_000, 1_292_528_932, 2023) for c in q["id"].split(",")]}
        elif path == "playlistItems":
            body = {"items": [{"contentDetails": {"videoId": "aaaaaaaaaaa"}}, {"contentDetails": {"videoId": "bbbbbbbbbbb"}}]}
        elif path == "videos":
            body = {"items": [_video("aaaaaaaaaaa", "Long one", 600_000), _video("bbbbbbbbbbb", "A short", 200_000, "PT45S")]}
        elif path == "search":
            body = {"items": [{"id": {"videoId": "aaaaaaaaaaa"}}]}
        elif path == "commentThreads":
            body = {"items": [{"snippet": {"topLevelComment": {"snippet": {"textDisplay": "works great", "likeCount": 5}}}}]}
        else:
            raise AssertionError(path)
        return Fetched(url=url, status=200, content_type="application/json", body=json.dumps(body))

    monkeypatch.setattr(youtube, "fetch", fake_fetch)
    youtube._cache.clear()
    return calls


@pytest.mark.parametrize("ref, vid", [
    ("https://www.youtube.com/watch?v=jNQXAC9IVRw", "jNQXAC9IVRw"),
    ("https://youtu.be/jNQXAC9IVRw?t=3", "jNQXAC9IVRw"),
    ("youtube.com/shorts/UgW60h_Tod4", "UgW60h_Tod4"),
    ("jNQXAC9IVRw", "jNQXAC9IVRw"),
    ("https://example.com/watch?v=jNQXAC9IVRw", None),
])
def test_video_ids_are_read_from_links(ref, vid):
    assert youtube.video_id(ref) == vid


def test_ours_means_the_company_channels_without_an_api_call(api):
    assert youtube.channel_ids("ours", KEY) == list(youtube.own_channels())
    assert youtube.channel_ids("gadget wiser", KEY) == ["UCakfrdHa0lItW8e5qpZd5lA"]
    assert youtube.channel_ids("https://www.youtube.com/channel/" + OURS, KEY) == [OURS]
    assert api == []
    assert youtube.channel_ids("https://www.youtube.com/@other/videos", KEY) == ["UC" + "o" * 22]


def test_channel_report_marks_our_channel_and_splits_shorts(api):
    [c] = youtube.channels([OURS], KEY)
    text = youtube.render_channel(c, youtube.recent_uploads(c, KEY, 2))
    assert "TechWiser (OUR channel)" in text and "Subscribers 2.6M" in text
    assert "long videos 600K (1)" in text and "Shorts 200K (1)" in text
    assert "https://www.youtube.com/watch?v=aaaaaaaaaaa" in text


def test_search_returns_links_and_stats_and_is_cached(api):
    first = youtube.render_videos(youtube.search("ai ads", KEY, order="viewCount", days=30))
    youtube.search("ai ads", KEY, order="viewCount", days=30)
    assert "https://www.youtube.com/watch?v=aaaaaaaaaaa" in first and "600K views" in first
    searches = [q for p, q in api if p == "search"]
    assert len(searches) == 1 and searches[0]["order"] == "viewCount" and "publishedAfter" in searches[0]


def test_errors_never_carry_the_key(monkeypatch):
    youtube._cache.clear()
    body = {"error": {"message": f"API key {KEY} not valid", "errors": [{"reason": "badRequest"}]}}
    monkeypatch.setattr(youtube, "fetch", lambda url, headers=None: Fetched(url, 400, "application/json", json.dumps(body)))
    with pytest.raises(youtube.YouTubeError) as e:
        youtube.search("x", KEY)
    assert KEY not in str(e.value)

    def boom(url, headers=None):
        raise RuntimeError(url)

    monkeypatch.setattr(youtube, "fetch", boom)
    with pytest.raises(youtube.YouTubeError) as e:
        youtube.channels([OURS + "x"], KEY)
    assert KEY not in str(e.value)


def test_quota_errors_are_explained(monkeypatch):
    youtube._cache.clear()
    body = {"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}}
    monkeypatch.setattr(youtube, "fetch", lambda url, headers=None: Fetched(url, 403, "application/json", json.dumps(body)))
    with pytest.raises(youtube.YouTubeError, match="daily quota"):
        youtube.search("x", KEY)


def test_tools_use_the_vault_key_and_mark_results_untrusted(api, monkeypatch):
    monkeypatch.setattr(tools_mod, "_youtube_key", lambda: KEY)
    monkeypatch.setattr(tools_mod, "_enforce", lambda *a, **k: None)
    state = {"messages": [], "subject_id": "u1", "subject_roles": ["researcher"], "scratchpad": {}}
    out = tools_mod.TOOLS["youtube_video"](state, videos="https://youtu.be/aaaaaaaaaaa", comments=1)
    assert out.startswith("<<UNTRUSTED CONTENT from YouTube") and "works great" in out and KEY not in out
    assert "TechWiser (OUR channel)" in tools_mod.TOOLS["youtube_channel"](state)


def test_without_a_key_the_tools_point_to_the_page_reader(monkeypatch):
    monkeypatch.setattr(tools_mod, "_youtube_key", lambda: None)
    monkeypatch.setattr(tools_mod, "_enforce", lambda *a, **k: None)
    state = {"messages": [], "subject_id": "u1", "subject_roles": ["researcher"], "scratchpad": {}}
    assert tools_mod.TOOLS["youtube_search"](state, query="x").startswith("ERROR: YouTube isn't connected")


def test_channel_refs_survive_leaked_arguments_and_lists(api):
    assert youtube.channel_ids("ours', videos:10", KEY) == list(youtube.own_channels())
    assert youtube.channel_ids("'TechWiser', https://www.youtube.com/@other", KEY) == [OURS, "UC" + "o" * 22]
