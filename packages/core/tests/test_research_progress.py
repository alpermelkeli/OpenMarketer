"""Tests for what a research run shows of its progress, node by node (``research_agent/progress``).

The states and changes are what ``research_agent/graph.py``'s nodes are given and answer with;
nothing runs.
"""

from typing import Any

from openmarketer_core.research.research_agent import graph  # noqa: F401  (declares the nodes)
from openmarketer_core.research.research_agent.definitions import agent
from openmarketer_core.research.research_agent.progress import FETCH_FAILED_NOTE, progress_of
from openmarketer_core.run_events import (
    FETCH_FAILED,
    FETCHED,
    PLANNED,
    READ,
    SCREENED,
    SEARCHED,
    SYNTHESIZED,
    RunEvent,
)


def state(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "round": 1,
        "queries": ["first query"],
        "next_query": 0,
        "categories": ["general"],
        "results": [],
        "next_result": 0,
        "pages": [],
        "next_page": 0,
        "readings": [],
        "findings": [],
        "notes": [],
    }
    return {**base, **changes}


def result(url: str) -> dict[str, Any]:
    return {"url": url, "title": "A title", "snippet": "a snippet", "query": "first query"}


def test_plan_shows_the_rounds_new_queries_and_its_kinds_of_source():
    update = {"round": 2, "queries": ["first query", "second", "third"], "categories": ["news"]}

    assert progress_of("plan", state(), update) == [
        RunEvent(PLANNED, {"round": 2, "queries": ["second", "third"], "categories": ["news"]})
    ]


def test_search_shows_its_query_and_how_many_results_it_found():
    before = state(results=[result("https://a.example")])
    update = {"results": [*before["results"], result("https://b.example")], "next_query": 1}

    assert progress_of("search", before, update) == [
        RunEvent(SEARCHED, {"query": "first query", "results": 1, "ok": True})
    ]


def test_search_that_did_not_answer_shows_no_results_and_not_why():
    update = {"next_query": 1, "notes": ["a search did not answer: provider text"]}

    assert progress_of("search", state(), update) == [
        RunEvent(SEARCHED, {"query": "first query", "results": 0, "ok": False})
    ]


def test_screen_shows_how_many_results_were_kept_and_dropped():
    fetched = result("https://old.example")
    candidates = [result(f"https://{n}.example") for n in "abc"]
    before = state(results=[fetched, *candidates], next_result=1)
    update = {"results": [fetched, candidates[0]], "notes": [], "cost_usd": 0.0}

    assert progress_of("screen", before, update) == [RunEvent(SCREENED, {"kept": 1, "dropped": 2})]


def test_screen_with_nothing_to_screen_shows_nothing():
    assert progress_of("screen", state(), {}) == []


def test_fetch_shows_the_pages_address_and_title():
    before = state(results=[result("https://a.example")])
    page = {"page_id": "p1", "url": "https://a.example/", "title": "A title", "flags": []}
    update = {"pages": [page], "next_result": 1}

    assert progress_of("fetch", before, update) == [
        RunEvent(FETCHED, {"url": "https://a.example/", "title": "A title"})
    ]


def test_fetch_that_failed_shows_the_address_and_the_handlers_sentence():
    before = state(results=[result("https://a.example")])
    update = {"next_result": 1, "notes": [f"{FETCH_FAILED_NOTE}the page answered HTTP 403"]}

    assert progress_of("fetch", before, update) == [
        RunEvent(FETCH_FAILED, {"url": "https://a.example", "reason": "the page answered HTTP 403"})
    ]


def test_read_shows_how_many_pages_have_been_read():
    before = state(readings=[{"page_id": "p1"}])
    update = {"readings": [{"page_id": "p1"}, {"page_id": "p2"}], "next_page": 2}

    assert progress_of("read", before, update) == [RunEvent(READ, {"pages": 2, "ok": True})]


def test_read_of_a_page_that_could_not_be_read_shows_it_was_not():
    update = {"next_page": 1, "notes": ["a stored page could not be read; it is left out"]}

    assert progress_of("read", state(), update) == [RunEvent(READ, {"pages": 0, "ok": False})]


def test_check_shows_how_many_checked_findings_the_run_has_after_the_round():
    update = {"findings": [{"statement": "one"}, {"statement": "two"}], "notes": [], "gaps": []}

    assert progress_of("check", state(round=2), update) == [
        RunEvent(SYNTHESIZED, {"findings": 2, "round": 2})
    ]


def test_synthesize_and_report_show_nothing():
    assert progress_of("synthesize", state(), {"drafts": [{"statement": "draft"}]}) == []
    assert progress_of("report", state(), {}) == []


def test_the_researcher_declares_its_progress():
    update = {"round": 1, "queries": ["first query"], "categories": ["general"]}

    assert agent.progress_of("plan", state(queries=[]), update) == (
        RunEvent(PLANNED, {"round": 1, "queries": ["first query"], "categories": ["general"]}),
    )
