"""The workspace's run events: the shape every route answers with, and the stream of them all.

A screen that must hear about a run wherever the person is (the dashboard's notifications) opens
one stream and follows every run event of the workspace's projects, oldest first, from a cursor or
from now. A chat's own streams stay what they are: the events of one chat and its runs, with
history pages, for the chat screen (``routes/chat.py``); this module owns the response shape both
use (``RunEventResponse``).

The stream reads the table about once a second, each read in a short transaction of its own: the
request's session is used only to answer where a cursor-less stream starts, before the first event
is sent. Events are for the operator's screen; a detail holds what the run wrote for it
(``openmarketer_core.run_events``) and nothing a model is given.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from openmarketer_api.dependencies import (
    DbSession,
    EventStreamsOpen,
    EventStreamTiming,
    Sessions,
)
from openmarketer_api.errors import json_problems
from openmarketer_api.event_stream import (
    EVENT_STREAM_MEDIA_TYPE,
    EventStreamResponse,
    StreamedEvent,
    StreamEnded,
    event_stream,
    json_line,
)
from openmarketer_api.identity import CurrentWorkspaceId
from openmarketer_api.pagination import HIGHEST_EVENT_ID
from openmarketer_core.db.run_events import (
    StoredRunEvent,
    events_of_workspace,
    latest_event_id_of_workspace,
)
from openmarketer_core.db.session import DatabaseError, transaction

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])

# The most events one read of a stream sends; a backlog is read again at once.
STREAM_READ_SIZE = 100

type EventDetailScalar = str | int | float | bool | None
type EventDetailValue = EventDetailScalar | list[EventDetailScalar]


class RunEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(
        description=(
            "The event's place in the order events were stored: larger for a later event."
            " It is the cursor a stream continues from (`after`, `Last-Event-ID`)."
        )
    )
    run_kind: str = Field(
        description=(
            "The kind of run that recorded it: `orchestrator-chat` for a turn of a chat,"
            " `research-run` for a research run."
        )
    )
    run_id: uuid.UUID = Field(description="The turn or the research run that recorded it.")
    kind: str = Field(
        description=("What happened, such as `tool_called`, `fetched` or `proposal_waiting`.")
    )
    detail: dict[str, EventDetailValue] = Field(
        description=(
            "What the screen shows of it: names, counts, identifiers, short texts; one flat"
            " level of values and lists of values."
        )
    )
    created_at: datetime


def run_event_response(stored: StoredRunEvent) -> RunEventResponse:
    """One stored event as every route answers with it."""
    return RunEventResponse(
        id=stored.id,
        run_kind=stored.run_kind,
        run_id=stored.run_id,
        kind=stored.kind,
        detail={key: _detail_value(value) for key, value in stored.detail.items()},
        created_at=stored.created_at,
    )


def _detail_value(value: Any) -> EventDetailValue:
    if isinstance(value, Sequence) and not isinstance(value, str):
        return list(value)
    return value


# One event of a stream, written as OpenAPI 3.2 describes a server-sent event.
STREAMED_EVENT_SCHEMA = {
    "type": "object",
    "required": ["id", "event", "data"],
    "properties": {
        "id": {"type": "string", "description": "The event's `id`."},
        "event": {"type": "string", "description": "The event's `kind`."},
        "data": {
            "type": "string",
            "contentMediaType": "application/json",
            "contentSchema": {"$ref": "#/components/schemas/RunEventResponse"},
        },
        "retry": {"type": "integer", "minimum": 0},
    },
}

EventCursor = Annotated[int, Query(ge=0, le=HIGHEST_EVENT_ID)]
EventCursorHeader = Annotated[int, Header(ge=0, le=HIGHEST_EVENT_ID)]


@router.get(
    "/stream",
    operation_id="streamWorkspaceEvents",
    status_code=200,
    response_class=EventStreamResponse,
    responses={
        200: {
            "description": (
                "Server-sent events, oldest first: every run event of the workspace's projects,"
                " each one `id: <id>`, `event: <kind>` and `data: <RunEventResponse as JSON>`;"
                " a `: keep-alive` comment is sent when nothing happened for a while, and the"
                " stream ends after a few minutes with a `retry:` hint sent at its start, so the"
                " client reconnects with `Last-Event-ID`."
            ),
            "content": {EVENT_STREAM_MEDIA_TYPE: {"itemSchema": STREAMED_EVENT_SCHEMA}},
        },
        # 403 and 503 too: the workspace router declares them in this route's media type.
        **json_problems(403, 503),
    },
)
def stream_events(
    request: Request,
    session: DbSession,
    sessions: Sessions,
    workspace_id: CurrentWorkspaceId,
    timing: EventStreamTiming,
    open_streams: EventStreamsOpen,
    after: EventCursor | None = None,
    last_event_id: Annotated[
        EventCursorHeader | None,
        Header(
            alias="Last-Event-ID",
            description=(
                "The `id` of the last event the client got, which a browser sends when it"
                " reconnects. It wins over `after`, which the reconnecting URL still holds."
            ),
        ),
    ] = None,
) -> EventStreamResponse:
    """The workspace's run events from a cursor, as server-sent events.

    `after` (or the `Last-Event-ID` header, which wins) is the `id` of the last event the client
    has; left out, the stream starts after the newest event, so a new client follows what happens
    from now on and not the whole history. 503 `too_many_streams` when the server has as many
    streams open as it keeps (the client tries again after a while). The stream ends after a few
    minutes, whether or not anything happened.
    """
    start_after = last_event_id if last_event_id is not None else after
    if start_after is None:
        start_after = latest_event_id_of_workspace(session, workspace_id=workspace_id)
    place = open_streams.take()

    def read_after(cursor: int | None) -> list[StreamedEvent]:
        try:
            with transaction(sessions) as reading:
                events = events_of_workspace(
                    reading,
                    workspace_id=workspace_id,
                    after=cursor,
                    limit=STREAM_READ_SIZE,
                )
        except DatabaseError as e:
            # The client reconnects after the stream's retry hint, and is answered 503 if the
            # database is still unavailable then.
            logger.warning("the workspace's events could not be read: %s", e)
            raise StreamEnded from e
        return [
            StreamedEvent(
                id=event.id,
                event=event.kind,
                data=json_line(run_event_response(event).model_dump(mode="json")),
            )
            for event in events
        ]

    return EventStreamResponse(
        event_stream(
            read_after,
            after=start_after,
            timing=timing,
            disconnected=request.is_disconnected,
        ),
        place=place,
    )
