"""Paging of the list routes: the ``limit`` and ``cursor`` query parameters.

A list answers with at most ``limit`` items, newest first, and ``next_cursor``:
null on the last page, otherwise the value to send as ``cursor`` for the items
that follow. A cursor is the position of the last item returned, written as
text, so the next page starts after that item whatever was added meanwhile. A
client passes it back unchanged and never builds one; a value this module did
not write is a request validation error (422).

The stores own the orderings and the queries (``openmarketer_core.db.pages``);
this module only moves a position in and out of a query string.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import Depends, Query
from fastapi.exceptions import RequestValidationError
from pydantic.json_schema import SkipJsonSchema

from openmarketer_core.db.pages import MAX_PAGE_SIZE, CreatedPosition

DEFAULT_PAGE_SIZE = 50
HIGHEST_VERSION = 2_147_483_647
# Run events are numbered by a BIGINT identity.
HIGHEST_EVENT_ID = 9_223_372_036_854_775_807

PageLimit = Annotated[
    int, Query(ge=1, le=MAX_PAGE_SIZE, description="The most items one response may hold.")
]
_Cursor = Annotated[
    str | SkipJsonSchema[None],
    Query(
        max_length=200,
        description="`next_cursor` of the previous page, unchanged. Leave out for the first page.",
    ),
]


def created_cursor(position: CreatedPosition | None) -> str | None:
    if position is None:
        return None
    text = f"{position.created_at.isoformat()}|{position.id}"
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def version_cursor(version: int | None) -> str | None:
    return None if version is None else str(version)


def event_cursor(event_id: int | None) -> str | None:
    return None if event_id is None else str(event_id)


def created_before(cursor: _Cursor = None) -> CreatedPosition | None:
    """The position a ``created_cursor`` was written from."""
    if cursor is None:
        return None
    try:
        text = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        created_at, _, row_id = text.partition("|")
        position = CreatedPosition(datetime.fromisoformat(created_at), uuid.UUID(row_id))
    except (ValueError, binascii.Error) as e:
        raise _not_a_cursor() from e
    if position.created_at.tzinfo is None:
        raise _not_a_cursor()
    return position


def version_before(cursor: _Cursor = None) -> int | None:
    """The version number a ``version_cursor`` was written from."""
    return _number_before(cursor, HIGHEST_VERSION)


def event_before(cursor: _Cursor = None) -> int | None:
    """The event identifier an ``event_cursor`` was written from."""
    return _number_before(cursor, HIGHEST_EVENT_ID)


def _number_before(cursor: str | None, highest: int) -> int | None:
    if cursor is None:
        return None
    if not (cursor.isascii() and cursor.isdigit() and 1 <= int(cursor) <= highest):
        raise _not_a_cursor()
    return int(cursor)


def _not_a_cursor() -> RequestValidationError:
    return RequestValidationError(
        [
            {
                "type": "value_error",
                "loc": ("query", "cursor"),
                "msg": "not a cursor this list returned",
            }
        ]
    )


CreatedBefore = Annotated[CreatedPosition | None, Depends(created_before)]
VersionBefore = Annotated[int | None, Depends(version_before)]
EventBefore = Annotated[int | None, Depends(event_before)]
