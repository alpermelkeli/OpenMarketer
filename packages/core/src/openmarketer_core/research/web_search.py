"""Searching the web through the installation's own SearXNG instance.

The researcher's ``search_web`` action goes through this module: a small client
for SearXNG's JSON API (``/search?format=json``, which its settings must
enable). The instance belongs to the installation and needs no key; nothing
here reaches a third-party search API and no query or result leaves the
installation's own services except through the engines SearXNG itself asks.

What a search engine answers is untrusted data: results are parsed into
``SearchResult``, each field bounded and only ``https`` addresses kept, and a
broken or hostile answer becomes ``SearchUnavailable`` with a message about the
instance and never a value it wrote. The client holds no state and no key, and
does not retry: the action that calls it is retried by the workflow, as every
action is.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

# What one search returns at most, and the bounds of each field of a result.
MAX_RESULTS = 5
MAX_URL_CHARS = 2048
MAX_TITLE_CHARS = 200
MAX_SNIPPET_CHARS = 300
DEFAULT_TIMEOUT_S = 20.0


class SearchUnavailable(Exception):
    """The search service did not answer, or answered something unreadable."""


@dataclass(frozen=True)
class SearchResult:
    """One result of a search: where it is, what it is called and what it says."""

    url: str
    title: str
    snippet: str


class SearchEngine(Protocol):
    """What the researcher's search action needs: one query in, its results out."""

    def search(
        self, query: str, *, categories: Sequence[str] = (), limit: int = MAX_RESULTS
    ) -> tuple[SearchResult, ...]: ...


class SearxngSearch:
    """The installation's SearXNG instance, over its JSON API."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        """``transport`` is for tests; production reaches the instance over the network."""
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._timeout_s = timeout_s

    def search(
        self, query: str, *, categories: Sequence[str] = (), limit: int = MAX_RESULTS
    ) -> tuple[SearchResult, ...]:
        """The results of ``query``, newest first as the instance ranks them.

        Raises ``SearchUnavailable`` when the instance does not answer with a
        readable result list of the expected shape.
        """
        params: dict[str, str] = {"q": query, "format": "json", "safesearch": "1", "pageno": "1"}
        if categories:
            params["categories"] = ",".join(sorted(categories))
        try:
            with httpx.Client(
                timeout=self._timeout_s, transport=self._transport, follow_redirects=False
            ) as client:
                response = client.get(f"{self._base_url}/search", params=params)
        except httpx.HTTPError as e:
            raise SearchUnavailable(
                f"the search service is unreachable ({type(e).__name__})"
            ) from e
        if response.status_code != 200:
            # The instance's own words go to the log; a message a run is shown holds none.
            logger.warning("search service answered HTTP %s", response.status_code)
            raise SearchUnavailable(f"the search service answered HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as e:
            raise SearchUnavailable("the search service answered something unreadable") from e
        if not isinstance(body, dict) or not isinstance(body.get("results"), list):
            raise SearchUnavailable("the search service answered something unreadable")
        results = [
            parsed
            for item in body["results"][: max(limit * 3, limit)]
            if (parsed := _result(item)) is not None
        ]
        return tuple(results[:limit])


def _result(item: Any) -> SearchResult | None:
    """One result of the instance's answer, bounded; ``None`` for an unusable entry."""
    if not isinstance(item, Mapping):
        return None
    url = item.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        # Only https pages are fetched (``page_fetch.py``): a result of another scheme is
        # dropped here rather than fetched and refused.
        return None
    return SearchResult(
        url=url[:MAX_URL_CHARS],
        title=_text(item.get("title"), MAX_TITLE_CHARS),
        snippet=_text(item.get("content"), MAX_SNIPPET_CHARS),
    )


def _text(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""
