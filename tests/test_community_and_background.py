"""Forums and code hosts (2026-11-05: the research agent found no app discussions at all: web search
was empty and Reddit refused fetch_page), research across many sites into one sheet, and any long
task in the background with a sheet or document at the end."""
from __future__ import annotations

import io
import json
import time

import pytest
from openpyxl import load_workbook

from app import background, jobs, research
from app.browsing import community
from app.browsing.fetch import Fetched
from app.browsing.search import Result
from app.orchestrator import tools as tools_mod

FEED = """<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><author><name>/u/ana</name></author><category term="androidapps"/>
<content type="html">&lt;p&gt;My finds: Gander, an offline file viewer.&lt;/p&gt; submitted by /u/ana</content>
<link href="https://www.reddit.com/r/androidapps/comments/1abc/finds/"/><published>2026-08-29T10:00:00+00:00</published>
<title>App finds of the year</title></entry></feed>"""


def _page(body: str, status: int = 200, ctype: str = "application/json") -> Fetched:
    return Fetched("https://x.example", status, ctype, body)


def test_filler_words_and_years_leave_the_query():
    assert community.keywords("new awesome underrated android apps 2026") == ["android", "apps"]
    assert community.keywords("self-hosted notes") == ["self-hosted", "notes"]


def test_reddit_feeds_parse_into_posts():
    [e] = community.parse_reddit_feed(FEED)
    assert e["title"] == "App finds of the year" and e["community"] == "r/androidapps" and e["author"] == "ana"
    assert "Gander" in e["text"] and "submitted by" not in e["text"] and e["date"] == "2026-08-29"


def test_reddit_says_rate_limited_once_and_does_not_retry(monkeypatch):
    calls = []
    monkeypatch.setattr(community, "REDDIT_GAP", 0)
    monkeypatch.setattr(community, "fetch", lambda url, headers=None: calls.append(url) or _page("", 429))
    community._cache.clear()
    with pytest.raises(community.RateLimited):
        community.reddit("android apps", subreddit="androidapps")
    assert len(calls) == 1


def test_reddit_answers_are_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(community, "REDDIT_GAP", 0)
    monkeypatch.setattr(community, "fetch", lambda url, headers=None: calls.append(url) or _page(FEED, 200, "application/atom+xml"))
    community._cache.clear()
    a = community.reddit("android apps")
    b = community.reddit("android apps")
    assert a == b and a[0].site == "Reddit" and len(calls) == 1


def test_hacker_news_loosens_a_query_that_finds_too_little(monkeypatch):
    seen = []

    def fake(url, headers=None):
        seen.append(url)
        hits = [] if "optionalWords" not in url else [
            {"objectID": "7", "title": "Show HN: Gander", "url": "https://g.example", "points": 90, "num_comments": 12,
             "created_at": "2026-07-19T00:00:00Z"}]
        return _page(json.dumps({"hits": hits}))

    monkeypatch.setattr(community, "fetch", fake)
    [p] = community.hackernews("offline file viewer android", launches=True)
    assert p.discussion.endswith("id=7") and p.score == 90 and "show_hn" in seen[0] and len(seen) == 2


def test_any_domain_is_searched_as_a_discourse_forum_or_through_web_search(monkeypatch):
    community._discourse.clear()

    def fake(url, headers=None):
        if url.startswith("https://forum.example/"):
            return _page(json.dumps({"topics": [{"id": 5, "slug": "best-apps", "title": "Best apps", "posts_count": 9}],
                                     "posts": [{"topic_id": 5, "blurb": "try Gander"}]}))
        return _page("<html>not discourse</html>", 200, "text/html")

    monkeypatch.setattr(community, "fetch", fake)
    web = lambda q: [Result("XDA thread", "https://xda.example/t/1", "apps", "")]  # noqa: E731
    posts, notes = community.search_all("android apps", ("forum.example", "xda.example"), "year", False, "", 5,
                                        web_search=web)
    sites = {p.site: p for p in posts}
    assert sites["forum.example"].discussion == "https://forum.example/t/best-apps/5" and sites["forum.example"].comments == 8
    assert sites["xda.example"].discussion == "https://xda.example/t/1" and not notes
    assert community._discourse == {"forum.example": True, "xda.example": False}


def test_unknown_sites_go_to_the_general_reader():
    with pytest.raises(community.NoAdapter):
        community.read("https://blog.example/2026/apps")


def test_read_thread_reads_any_link(monkeypatch):
    monkeypatch.setattr(tools_mod, "_enforce", lambda *a: None)
    monkeypatch.setattr(tools_mod, "_community_keys", lambda: (None, None))
    monkeypatch.setattr(tools_mod, "_page_text", lambda url, max_chars=0: f"PAGE: blog\nURL: {url}\n\ntext")
    assert "PAGE: blog" in tools_mod.read_thread({"subject_id": "u"}, "https://blog.example/post")


def test_merging_ranks_things_by_how_many_sources_mention_them():
    rows = [{"Name": "Gander", "What": "viewer", "_source": "a"},
            {"Name": "gander - offline viewer", "What": "offline file viewer, no permissions", "_source": "b"},
            {"Name": "Other", "What": "x", "_source": "a"}, {"Name": "", "_source": "c"}]
    out = research.merge(rows, "Name", ["Name", "What"])
    assert [r["Name"] for r in out] == ["gander - offline viewer", "Other"]
    assert out[0]["Mentions"] == 2 and out[0]["What"] == "offline file viewer, no permissions"
    assert out[0]["Sources"] == "a\nb"


def test_collect_reads_sources_and_skips_ones_that_fail():
    sources = [research.Source("https://a", "A", "forum", 3), research.Source("https://b", "B", "web", 2)]
    listings = [research.Source("GitHub", "repos", "listing", 0, "1. owner/gander ★900 https://github.com/o/gander")]
    reader = lambda url: "ERROR: blocked" if url == "https://b" else "Gander is great"  # noqa: E731
    extract = lambda text, url: [{"Name": "Gander", "Link": "https://github.com/o/gander" if url == "GitHub" else ""}]  # noqa: E731
    rep = research.collect("android apps", "apps", ["Name", "Link"], sources, listings, reader, extract, lambda t: None)
    assert [s.url for s in rep.read] == ["https://a"] and [s.url for s in rep.failed] == ["https://b"]
    assert rep.rows[0]["Mentions"] == 2 and rep.columns == ["Name", "Link", "Mentions", "Sources"]


class FakeReply(str):
    tool_calls = None


def test_a_background_task_uses_tools_then_writes_rows(monkeypatch):
    from app.orchestrator import nodes

    monkeypatch.setattr(nodes, "_system_prompt", lambda state: "sys")
    monkeypatch.setattr(nodes, "tools_for", lambda dept: {"weather": lambda state, location: f"sunny in {location}"})
    monkeypatch.setattr(background, "MAX_ROUNDS", 5)
    replies = iter([
        json.dumps({"tool": "weather", "args": {"location": "Pune"}}),
        json.dumps({"tool": "send_mail", "args": {"to": "x@y.z"}}),  # needs a person: refused
        "Done.",
        json.dumps([{"City": "Pune", "Weather": "sunny", "Source": "weather"}]),
    ])
    seen = []

    def chat(messages, **kw):
        seen.append(messages)
        return FakeReply(next(replies))

    out = background.run({"subject_id": "u", "scratchpad": {}}, "weather in Pune as a sheet", "sheet", [], lambda t: None, chat)
    assert out.rows == [{"City": "Pune", "Weather": "sunny", "Source": "weather"}] and out.calls == 2
    tool_msgs = [m for m in seen[2] if m.get("role") == "tool"]
    assert "sunny in Pune" in tool_msgs[0]["content"] and "not available" in tool_msgs[1]["content"]


def test_finished_jobs_can_email_and_set_a_reminder(monkeypatch):
    sent, reminders, bells = [], [], []

    class S:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, model, key):
            return object()

    import app.db.session as dbs
    from app.reminders import delivery, service

    monkeypatch.setattr(dbs, "SessionLocal", S)
    monkeypatch.setattr(service, "notify", lambda s, o, k, t, b="": bells.append(t))
    monkeypatch.setattr(service, "create", lambda s, o, when, text, **kw: reminders.append((when, text)))
    monkeypatch.setattr(delivery, "email_person", lambda s, u, t, b: sent.append(t) or True)
    jobs._notify("u1", "Ready: Research: android apps", "saved", jobs.Alerts(email=True, remind_at="tomorrow 9am"))
    assert bells == ["Ready: Research: android apps"] and sent == ["Ready: Research: android apps"]
    assert reminders == [("tomorrow 9am", "Look at: Research: android apps")]
    jobs._notify("u1", "Couldn't finish: x", "boom", jobs.Alerts(remind_at="tomorrow 9am"))
    assert len(reminders) == 1  # no reminder to look at a failure


@pytest.fixture
def saved(monkeypatch):
    out, notes = {}, []
    monkeypatch.setattr(tools_mod, "_enforce", lambda *a: None)
    monkeypatch.setattr(tools_mod, "_private", lambda state: True)
    monkeypatch.setattr(tools_mod, "_save", lambda state, path, data, **kw: out.update({path: data}) or f"Created '{path}' (1 KB).")
    monkeypatch.setattr(jobs, "_notify", lambda owner, title, body, alerts=None: notes.append((owner, title, body, alerts)))
    jobs._jobs.clear()
    return out, notes


def test_save_table_writes_a_real_sheet(saved):
    out, _ = saved
    rows = json.dumps([{"App": "Gander", "Special": ["offline", "no permissions"]}, {"App": "=cmd", "Stars": 5}])
    msg = tools_mod.save_table({"subject_id": "u"}, "Research/apps", rows)
    assert "2 rows" in msg and "My files" in msg
    data = list(load_workbook(io.BytesIO(out["Research/apps.xlsx"]))["Data"].iter_rows(values_only=True))
    assert data[0] == ("App", "Special", "Stars") and data[1][1] == "offline; no permissions"
    assert data[2][0] == "'=cmd"  # never a formula
    assert tools_mod.save_table({"subject_id": "u"}, "x", "not json").startswith("ERROR")


def test_web_research_runs_in_the_background_and_saves_a_ranked_sheet(saved, monkeypatch):
    out, notes = saved
    monkeypatch.setattr(tools_mod, "_search_api", lambda: None)
    monkeypatch.setattr(tools_mod, "_community_keys", lambda: (None, None))
    monkeypatch.setattr(research, "find_sources", lambda *a, **k: (
        [research.Source("https://a", "A", "forum", 3), research.Source("https://b", "B", "web", 2)], [], []))
    monkeypatch.setattr(community, "read", lambda url, *a: (_ for _ in ()).throw(community.NoAdapter(url)))
    monkeypatch.setattr(tools_mod, "_page_text", lambda url, max_chars=0: f"text of {url}")
    from app import bigsummary

    monkeypatch.setattr(bigsummary, "_ask", lambda system, user, n, chat=None: json.dumps(
        [{"Name": "Gander", "What makes it special": "offline"}] + ([{"Name": "Zen"}] if "https://b" in user else [])))
    msg = tools_mod.web_research({"subject_id": "u9"}, "android apps", what="apps", sites="reddit, xda-developers.com, web")
    assert "STARTED IN THE BACKGROUND" in msg and "xda-developers.com" in msg
    for _ in range(200):
        if notes:
            break
        time.sleep(0.05)
    assert notes and notes[0][1].startswith("Ready"), notes
    path = next(iter(out))
    rows = list(load_workbook(io.BytesIO(out[path]))["Data"].iter_rows(values_only=True))
    assert rows[1][0] == "Gander" and rows[1][-2] == 2 and rows[2][0] == "Zen"


def test_start_background_task_saves_a_document(saved, monkeypatch):
    out, notes = saved
    monkeypatch.setattr(background, "run", lambda state, task, output, cols, progress: background.Outcome(
        "# Report\n\nAll done.", [], [], 3, 7))
    msg = tools_mod.start_background_task({"subject_id": "u7", "messages": [{"role": "user"}]},
                                          "compare 20 note apps", output="word", email=True)
    assert "STARTED IN THE BACKGROUND" in msg and ".docx" in msg and "email" in msg
    for _ in range(200):
        if notes:
            break
        time.sleep(0.05)
    assert notes[0][1].startswith("Ready") and any(p.endswith(".docx") for p in out)
    assert notes[0][3] == jobs.Alerts(email=True)


def test_the_model_picks_sources_that_name_items_over_popular_news():
    srcs = [research.Source(f"https://s{i}", f"title {i}", "forum", 10 - i) for i in range(10)]
    picked = research.choose(srcs, 3, lambda listing: "7, 8, 2")
    assert [s.url for s in picked] == ["https://s7", "https://s8", "https://s2"]
    assert [s.url for s in research.choose(srcs, 3, lambda listing: "no idea")] == ["https://s0", "https://s1", "https://s2"]
    assert research.merge([{"Name": "A", "Price": "None", "_source": "x"}, {"Name": "a", "Price": "Free", "_source": "y"}],
                          "Name", ["Name", "Price"])[0]["Price"] == "Free"
