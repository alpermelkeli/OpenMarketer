"""Tests for run events as plain data: what an event may hold, and how it is bounded."""

import math

import pytest

from openmarketer_core.run_events import (
    MAX_DETAIL_KEYS,
    MAX_LIST_ITEMS,
    MAX_TEXT_LENGTH,
    SEARCHED,
    TOOL_CALLED,
    InvalidRunEvent,
    RunEvent,
    cut_text,
)


def test_an_event_keeps_short_structured_values_as_they_are():
    event = RunEvent(SEARCHED, {"query": "pricing pages", "results": 7, "ok": True, "x": None})

    assert event.detail == {"query": "pricing pages", "results": 7, "ok": True, "x": None}


def test_an_event_without_detail_has_an_empty_one():
    assert RunEvent(TOOL_CALLED).detail == {}


def test_a_long_text_value_is_cut_with_an_ellipsis():
    event = RunEvent(SEARCHED, {"query": "q" * 1000})

    query = event.detail["query"]
    assert isinstance(query, str)
    assert len(query) == MAX_TEXT_LENGTH
    assert query.endswith("…")


def test_a_text_at_the_limit_is_kept_whole():
    assert cut_text("q" * MAX_TEXT_LENGTH) == "q" * MAX_TEXT_LENGTH


def test_a_list_keeps_its_first_items_each_cut():
    event = RunEvent("planned", {"queries": ["q" * 1000] * (MAX_LIST_ITEMS + 5)})

    queries = event.detail["queries"]
    assert isinstance(queries, list)
    assert len(queries) == MAX_LIST_ITEMS
    assert all(isinstance(q, str) and len(q) == MAX_TEXT_LENGTH for q in queries)


def test_a_tuple_is_held_as_a_list():
    assert RunEvent("planned", {"categories": ("news", "general")}).detail == {
        "categories": ["news", "general"]
    }


def test_the_detail_is_a_copy_of_what_was_passed():
    detail = {"query": "pricing"}
    event = RunEvent(SEARCHED, detail)

    detail["query"] = "changed"

    assert event.detail == {"query": "pricing"}


def test_too_many_keys_are_refused():
    with pytest.raises(InvalidRunEvent):
        RunEvent(SEARCHED, {f"key_{i}": i for i in range(MAX_DETAIL_KEYS + 1)})


@pytest.mark.parametrize(
    "value",
    [
        {"nested": "mapping"},
        [["nested", "list"]],
        b"bytes",
        object(),
        math.nan,
        math.inf,
    ],
)
def test_a_value_json_cannot_hold_flat_is_refused(value):
    with pytest.raises(InvalidRunEvent):
        RunEvent(SEARCHED, {"value": value})


@pytest.mark.parametrize("key", ["", "Query", "has space", "k" * 41, "1st"])
def test_a_key_that_is_not_a_short_lower_case_name_is_refused(key):
    with pytest.raises(InvalidRunEvent):
        RunEvent(SEARCHED, {key: 1})


@pytest.mark.parametrize("kind", ["", "Searched", "tool-called", "k" * 41])
def test_a_kind_that_is_not_a_short_lower_case_name_is_refused(kind):
    with pytest.raises(InvalidRunEvent):
        RunEvent(kind)
