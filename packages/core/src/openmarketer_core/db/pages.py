"""Pages: how the list queries of the stores are bounded.

Every list is ordered newest first and read one page at a time. A page says
where the next one starts (``next_before``), and the next request passes that
position back: rows are selected by comparing with it, never by counting an
offset, so a row inserted between two requests neither repeats a row nor hides
one. A row added at the head after the first page was read is not seen by that
walk; reading from the start again shows it.

This module holds no query. Each store builds its own and owns its ordering.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

MAX_PAGE_SIZE = 100


@dataclass(frozen=True)
class CreatedPosition:
    """A place in a list ordered by creation time, newest first.

    Rows of one transaction share a creation time, so the identifier breaks the tie.
    """

    created_at: datetime
    id: uuid.UUID


@dataclass(frozen=True)
class Page[Item, Position]:
    """Some rows of a list, and the position to pass to read the rows after them."""

    items: tuple[Item, ...]
    next_before: Position | None


def rows_to_fetch(limit: int) -> int:
    """How many rows a query asks for: one more than the page, to learn whether more follow."""
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"a page holds between 1 and {MAX_PAGE_SIZE} rows, not {limit}")
    return limit + 1


def page_of[Item, Position](
    fetched: Sequence[Item], limit: int, position: Callable[[Item], Position]
) -> Page[Item, Position]:
    """The page made of the first ``limit`` of the rows a query for one more returned."""
    items = tuple(fetched[:limit])
    more_follow = len(fetched) > limit
    return Page(items=items, next_before=position(items[-1]) if more_follow else None)
