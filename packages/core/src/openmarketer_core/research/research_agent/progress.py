"""What a research run shows of its progress, node by node, to the person watching its chat.

``progress_of`` is the researcher's progress function (``Agent.progress``): from the name of
a node of ``graph.py``, the state it ran on and the changes it answered with, it gives that
step's events (``openmarketer_core.run_events``):

- ``plan``: ``planned`` with the round's new queries and the kinds of source it searches;
- ``search``: ``searched`` with the query, how many results it found, and whether it answered;
- ``screen``: ``screened`` with how many results were kept and dropped;
- ``fetch``: ``fetched`` with the page's address and title, or ``fetch_failed`` with the
  address and the handler's sentence of why;
- ``read``: ``read`` with how many pages have been read so far, and whether this one was;
- ``check``: ``synthesized`` with how many checked findings the run has after the round.

``synthesize`` and ``report`` show nothing: the first's drafts are not findings until
``check`` has checked them, and the run's end (``finished``, ``failed``) is the workflow's to
record. Only structured data is shown: counts, the queries a model wrote, a page's address
and its title, cut short (``RunEvent``). A title is what a web page says of itself, shown as
plain text and never read by a model; no page text, snippet or quote is ever shown.

This module is pure: it reads what it is given and imports no framework.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

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

# How ``graph.fetch`` notes a page that was not fetched, before the handler's sentence of why.
FETCH_FAILED_NOTE = "a page was not fetched: "


def progress_of(
    node: str, state: Mapping[str, Any], update: Mapping[str, Any]
) -> Sequence[RunEvent]:
    """The events of the node ``node`` of a research run, which ran on ``state``."""
    shown = _SHOWN.get(node)
    return [] if shown is None else shown(state, update)


def _planned(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    fresh = update["queries"][len(state["queries"]) :]
    detail = {"round": update["round"], "queries": fresh, "categories": update["categories"]}
    return [RunEvent(PLANNED, detail)]


def _searched(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    query = state["queries"][state["next_query"]]
    answered = "results" in update
    found = len(update["results"]) - len(state["results"]) if answered else 0
    return [RunEvent(SEARCHED, {"query": query, "results": found, "ok": answered})]


def _screened(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    if "results" not in update:
        return []
    candidates = len(state["results"]) - state["next_result"]
    kept = len(update["results"]) - state["next_result"]
    return [RunEvent(SCREENED, {"kept": kept, "dropped": candidates - kept})]


def _fetched(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    if "pages" in update:
        page = update["pages"][-1]
        return [RunEvent(FETCHED, {"url": page["url"], "title": page["title"]})]
    url = state["results"][state["next_result"]]["url"]
    return [RunEvent(FETCH_FAILED, {"url": url, "reason": _fetch_failure(update)})]


def _fetch_failure(update: Mapping[str, Any]) -> str:
    notes = update.get("notes") or [""]
    note = notes[-1]
    return note.removeprefix(FETCH_FAILED_NOTE) if note.startswith(FETCH_FAILED_NOTE) else ""


def _read(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    was_read = "readings" in update
    pages = len(update["readings"] if was_read else state["readings"])
    return [RunEvent(READ, {"pages": pages, "ok": was_read})]


def _checked(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    detail = {"findings": len(update["findings"]), "round": state["round"]}
    return [RunEvent(SYNTHESIZED, detail)]


_SHOWN = {
    "plan": _planned,
    "search": _searched,
    "screen": _screened,
    "fetch": _fetched,
    "read": _read,
    "check": _checked,
}
