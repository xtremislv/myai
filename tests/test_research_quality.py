"""Keyless search keeps only relevant results, block pages are never passed off as content,
and agents open sources before answering from search snippets."""
from __future__ import annotations

from app.browsing import extract, search
from app.browsing.search import Result
from app.orchestrator import nodes


def test_results_matching_one_word_are_dropped_and_the_best_come_first():
    junk = [Result("SOLID Principles with Real Life Examples", "https://g.example/solid", "design"),
            Result("Solid - Wikipedia", "https://w.example/Solid", "state of matter"),
            Result("Solid-state battery makers race to commercialization", "https://n.example/a", "batteries"),
            Result("Solid-state batteries: commercialization timeline", "https://n.example/b", "")]
    kept = search.relevant("solid state battery commercialization", junk)
    assert [r.url for r in kept] == ["https://n.example/a", "https://n.example/b"]


def test_plurals_and_variants_still_count():
    r = Result("Lithium-ion batteries get cheaper", "https://x.example", "")
    assert search.relevance("lithium battery", r) == 1.0


def test_block_and_challenge_pages_are_recognised_but_articles_are_not():
    assert extract.block_reason("Access Denied", "You don't have permission to access this page")
    assert extract.block_reason("Just a moment...", "Checking your browser")
    assert extract.block_reason("Error | Britannica", "Ask the Chatbot Games & Quizzes")
    article = "A long article about how an access denied error happens. " * 60
    assert extract.block_reason("Why access is denied", article) is None


def test_scholarly_queries_lose_the_years_the_model_adds(monkeypatch):
    seen = []
    monkeypatch.setattr(search, "SCHOLARLY", {"arxiv": lambda q, n: seen.append(q) or []})
    search.scholarly("battery degradation modelling 2024 2025 2026", sources=("arxiv",))
    assert seen == ["battery degradation modelling"]


def test_answers_from_snippets_alone_get_sent_to_read_the_sources():
    turn = [{"role": "assistant", "tool_calls": [{}]}, {"role": "tool", "name": "web_search", "content": "..."}]
    assert nodes.searched_without_reading(turn)
    assert not nodes.searched_without_reading(turn + [{"role": "tool", "name": "fetch_page", "content": "..."}])
    assert not nodes.searched_without_reading([{"role": "tool", "name": "scholar_search", "content": "..."}])
    messages = [{"role": "user", "content": "old"}, *turn, {"role": "user", "content": "new"}]
    assert nodes.this_turn(messages) == []
