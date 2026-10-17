"""Run events: what a run did, step by step, for the person who watches it.

A run of any kind (a chat turn, a research run) records short events as it
goes: which tool the orchestrator called, which queries a research run planned,
which page it fetched. They are stored (``db/run_events.py``) and shown live in
the chat thread that started the run. They are for the person's screen only:
nothing here is ever given to a model.

An event holds structured data only: names, counts, identifiers, the queries a
model wrote, a page's address and title. Page text and quotes never go into an
event, and a ``reason`` is a sentence already worded for the operator, never
what a provider wrote. ``RunEvent`` bounds what it holds when it is built, so
nothing unbounded reaches the table: a text value is cut, a list keeps its
first items, a detail has few keys, and a value that JSON cannot hold is
refused. Nested mappings are refused too: a detail is one flat level of
scalars and lists of scalars, which is all a screen line needs.

This module is plain data. It stores nothing, and it does not decide which
events a run records; the run's own rules do.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

type JsonScalar = str | int | float | bool | None
# A list of scalars is any sequence when an event is built, and a list once it is built.
type JsonValue = JsonScalar | Sequence[JsonScalar]

# A text value longer than this is cut, and ends with an ellipsis.
MAX_TEXT_LENGTH = 300
# A list keeps its first items only.
MAX_LIST_ITEMS = 10
# A detail holds at most this many keys, each a short identifier written by code.
MAX_DETAIL_KEYS = 8
MAX_KEY_LENGTH = 40
_NAME = re.compile(r"[a-z][a-z0-9_]*")
_ELLIPSIS = "…"

# ------------------------------------------------------------- chat turn
TURN_STARTED = "turn_started"
TOOL_CALLED = "tool_called"  # {tool, label}
TOOL_ANSWERED = "tool_answered"  # {tool, ok}
RUN_STARTED = "run_started"  # {run_kind, run_id}
TURN_ANSWERED = "turn_answered"  # {message_id}
TURN_FAILED = "turn_failed"  # {reason}

# ---------------------------------------------------------- research run
PLANNED = "planned"  # {queries, categories}
SEARCHED = "searched"  # {query, results}
SCREENED = "screened"  # {kept, dropped}
FETCHED = "fetched"  # {url, title}
FETCH_FAILED = "fetch_failed"  # {url, reason}
READ = "read"  # {pages}
SYNTHESIZED = "synthesized"  # {findings, round}
FINISHED = "finished"  # {findings}
FAILED = "failed"  # {reason}


class InvalidRunEvent(ValueError):
    """An event that code built wrongly: a bad kind or key, too many keys, or a non-JSON value."""


@dataclass(frozen=True)
class RunEvent:
    """One thing a run did, as the person watching it is shown it.

    ``kind`` is one of the names above (or another lower-case name of a run's
    own); ``detail`` is bounded when the event is built, and is a copy: the
    mapping the caller passed is not kept.
    """

    kind: str
    detail: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_name(self.kind, "kind")
        object.__setattr__(self, "detail", _bounded_detail(self.detail))


def cut_text(text: str) -> str:
    """``text`` as an event holds it: at most ``MAX_TEXT_LENGTH`` characters."""
    if len(text) <= MAX_TEXT_LENGTH:
        return text
    return text[: MAX_TEXT_LENGTH - len(_ELLIPSIS)] + _ELLIPSIS


def _bounded_detail(detail: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    if len(detail) > MAX_DETAIL_KEYS:
        raise InvalidRunEvent(f"a detail holds at most {MAX_DETAIL_KEYS} keys, not {len(detail)}")
    bounded: dict[str, JsonValue] = {}
    for key, value in detail.items():
        _check_name(key, "key")
        bounded[key] = _bounded_value(key, value)
    return bounded


def _bounded_value(key: str, value: object) -> JsonValue:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        return _bounded_scalar(key, value)
    return [_bounded_scalar(key, item) for item in list(value)[:MAX_LIST_ITEMS]]


def _bounded_scalar(key: str, value: object) -> JsonScalar:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidRunEvent(f"{key!r} is not a finite number")
        return value
    if isinstance(value, str):
        return cut_text(value)
    raise InvalidRunEvent(f"{key!r} holds a {type(value).__name__}, which an event cannot hold")


def _check_name(name: object, what: str) -> None:
    if not isinstance(name, str) or len(name) > MAX_KEY_LENGTH or not _NAME.fullmatch(name):
        raise InvalidRunEvent(
            f"an event's {what} is a lower-case name of at most {MAX_KEY_LENGTH} characters"
        )
