"""Chats: the operator's named conversations with the orchestrator about one project.

The operator opens a chat and sends it messages; the chat agent answers each in
this process - it reads the project's stored records and returns one answer, and
it may start a research run when the operator asks for research - and the
exchange is stored under the chat. One route opens a chat, one lists the
project's chats, most recently active first, one reads a chat, one pages a
chat's messages, newest first, one sends a message and one records that the
operator has read a chat. Two read a chat's events, what its turns and the
research runs it started did step by step: one pages them, newest first, for
the history a screen loads, and one streams those stored after a cursor as
server-sent events (``event_stream.py``) while the screen is open. Nothing
else changes the project, and no route presents a chat as approved by anyone:
the agent reads and answers and starts research runs, and approvals, settings
and every other kind of run stay on their own routes.

Unlike an analysis, which a worker runs, a chat turn runs in the API's process,
so this module resolves the turn's model itself: the workspace's choice for the
``orchestrator`` role (``model_choices``) with the provider's key
(``credentials.resolution.model_key``). ``chat_model`` is a dependency, so a
test can hand the route a scripted model instead. The turn's request action
(reaching ``PostgresChatRequests``) is wired from the request's session factory
and the same workflow starter the research route uses. The turn is traced like
an agent's run when the process traced (``Tracing.traced``): a turn runs in a
worker thread of this server, under the graph handler for the turn's own
context, and its model is wrapped so each call is a generation.

A turn that fails stores nothing (``orchestrator/ask.py``), so the same message
may be sent again; this module words no failure of the agent's.

The stream reads the table about once a second, each read in a short
transaction of its own: the request's session ends when the route returns,
before the first event is sent, and is used only to answer a missing chat with
a 404. Events are for the operator's screen; a detail holds what the run wrote
for it (``openmarketer_core.run_events``) and nothing a model is given.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from openmarketer_api.chat_requests import PostgresChatRequests
from openmarketer_api.dependencies import (
    DbSession,
    Environment,
    EventStreamsOpen,
    EventStreamTiming,
    Models,
    ProcessTracing,
    Sessions,
    StartWorkflow,
    StoringCipher,
    no_request_body,
)
from openmarketer_api.errors import json_problems, problems
from openmarketer_api.event_stream import (
    EVENT_STREAM_MEDIA_TYPE,
    EventStreamResponse,
    StreamedEvent,
    StreamEnded,
    event_stream,
    json_line,
)
from openmarketer_api.identity import CurrentWorkspaceId
from openmarketer_api.pagination import (
    DEFAULT_PAGE_SIZE,
    HIGHEST_EVENT_ID,
    CreatedBefore,
    EventBefore,
    PageLimit,
    created_cursor,
    event_cursor,
)
from openmarketer_core.credentials.resolution import model_key
from openmarketer_core.db.chat import (
    ChatMessage,
    ChatNotFound,
    ChatThread,
    create_chat,
    get_chat,
    list_chat_messages,
    list_chats,
    mark_chat_read,
    require_chat,
)
from openmarketer_core.db.models import ChatRole
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.run_events import StoredRunEvent, events_of_chat, latest_events_of_chat
from openmarketer_core.db.session import DatabaseError, transaction
from openmarketer_core.llm import ChatModel, RouterChatModel
from openmarketer_core.orchestrator.ask import ask_project
from openmarketer_core.orchestrator.reads_postgres import PostgresChatReads
from openmarketer_core.workspace_settings.model_settings import model_choices

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}/chats", tags=["chat"])

MAX_TITLE_LENGTH = 200
MAX_MESSAGE_LENGTH = 10_000
# The most events one read of a stream sends; a backlog is read again at once.
STREAM_READ_SIZE = 100

type EventDetailScalar = str | int | float | bool | None
type EventDetailValue = EventDetailScalar | list[EventDetailScalar]


class CreateChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TITLE_LENGTH),
        ]
        | None
    ) = Field(
        default=None,
        description="What the chat is called. Left out, it is named after the first message.",
    )


class ChatThreadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    created_at: datetime
    last_activity_at: datetime = Field(
        description="When the chat last gained a message; the list orders by it."
    )
    last_read_at: datetime | None = Field(
        description="When the operator last read the chat; null when it was never read."
    )
    unread: bool = Field(description="Whether a message was written after `last_read_at`.")
    busy: bool = Field(
        description=(
            "Whether a turn of the chat is running now; a message sent meanwhile is a 409"
            " `chat_busy`."
        )
    )


class ChatThreadListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chats: list[ChatThreadResponse]
    next_cursor: str | None = Field(
        description="Null on the last page; otherwise the `cursor` of the next request."
    )


class ChatMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_LENGTH),
    ]


class ChatMessageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    project_id: uuid.UUID
    chat_id: uuid.UUID
    turn_id: uuid.UUID | None = Field(
        description=(
            "The turn that wrote it, whose events have it as `run_id` with `run_kind`"
            " `orchestrator-chat`; null for a message stored before turns were."
        )
    )
    role: ChatRole = Field(
        description="Who wrote it: `user` for the operator, `assistant` for the chat agent."
    )
    content: str
    created_at: datetime


class ChatMessageListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessageResponse]
    next_cursor: str | None = Field(
        description="Null on the last page; otherwise the `cursor` of the next request."
    )


class ChatEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(
        description=(
            "The event's place in the order events were stored: larger for a later event."
            " It is the cursor a stream continues from (`after`, `Last-Event-ID`)."
        )
    )
    run_kind: str = Field(
        description=(
            "The kind of run that recorded it: `orchestrator-chat` for a turn of the chat,"
            " `research-run` for a research run the chat started."
        )
    )
    run_id: uuid.UUID = Field(description="The turn or the research run that recorded it.")
    kind: str = Field(description="What happened, such as `tool_called` or `fetched`.")
    detail: dict[str, EventDetailValue] = Field(
        description=(
            "What the screen shows of it: names, counts, identifiers, short texts; one flat"
            " level of values and lists of values."
        )
    )
    created_at: datetime


class ChatEventListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[ChatEventResponse] = Field(description="Newest first.")
    next_cursor: str | None = Field(
        description="Null on the last page; otherwise the `cursor` of the next request."
    )


class ChatTurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: ChatMessageResponse
    answer: ChatMessageResponse = Field(description="The answer the agent submitted.")
    model: str = Field(description="The model that answered, as the provider named it.")
    cost_usd: float = Field(description="What the whole turn cost, a summarised history included.")


def chat_model(
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    models: Models,
    environ: Environment,
    cipher: StoringCipher,
    tracing: ProcessTracing,
) -> ChatModel:
    """The model one chat turn calls: the workspace's choice of the role, with its key.

    Wrapped for tracing when the process traces, so each call is a generation, as a worker's
    attempts are. Raises ``StoredModelChoiceInvalid`` when the workspace's stored choice broke a
    rule, ``ModelKeyNotSet`` when the key of the provider's variable is neither in
    the environment nor stored, and ``CredentialUnreadable`` when a stored key
    cannot be opened.
    """
    chosen = model_choices(session, models, workspace_id=workspace_id).router_for(models)
    key = model_key(session, environ, workspace_id=workspace_id, router=chosen, cipher=cipher)
    return tracing.model(RouterChatModel(router=chosen, api_key=key))


def _thread(stored: ChatThread) -> ChatThreadResponse:
    return ChatThreadResponse(
        id=stored.id,
        project_id=stored.project_id,
        title=stored.title,
        created_at=stored.created_at,
        last_activity_at=stored.last_activity_at,
        last_read_at=stored.last_read_at,
        unread=stored.unread,
        busy=stored.busy,
    )


def _message(stored: ChatMessage) -> ChatMessageResponse:
    return ChatMessageResponse(
        id=stored.id,
        project_id=stored.project_id,
        chat_id=stored.thread_id,
        turn_id=stored.turn_id,
        role=stored.role,
        content=stored.content,
        created_at=stored.created_at,
    )


@router.post(
    "",
    operation_id="createChat",
    status_code=201,
    response_model=ChatThreadResponse,
    responses=problems(404),
)
def create(
    project_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    body: CreateChatRequest | None = None,
) -> ChatThreadResponse:
    """Open an empty chat about the project, named by the body or after its first message."""
    chat = create_chat(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        title=None if body is None else body.title,
    )
    return _thread(chat)


@router.get(
    "", operation_id="listChats", response_model=ChatThreadListResponse, responses=problems(404)
)
def list_all(
    project_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    before: CreatedBefore,
    limit: PageLimit = DEFAULT_PAGE_SIZE,
) -> ChatThreadListResponse:
    """The project's chats as they are now, most recently active first, one page at a time."""
    page = list_chats(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        limit=limit,
        before=before,
    )
    return ChatThreadListResponse(
        chats=[_thread(chat) for chat in page.items],
        next_cursor=created_cursor(page.next_before),
    )


@router.get(
    "/{chat_id}",
    operation_id="getChat",
    response_model=ChatThreadResponse,
    responses=problems(404),
)
def get(
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
) -> ChatThreadResponse:
    """One chat as it is now, with whether it has messages the operator has not read."""
    return _thread(
        get_chat(session, workspace_id=workspace_id, project_id=project_id, chat_id=chat_id)
    )


@router.get(
    "/{chat_id}/messages",
    operation_id="listChatMessages",
    response_model=ChatMessageListResponse,
    responses=problems(404),
)
def list_messages(
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    before: CreatedBefore,
    limit: PageLimit = DEFAULT_PAGE_SIZE,
) -> ChatMessageListResponse:
    """The chat's messages as they are now, newest first, one page at a time."""
    page = list_chat_messages(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        chat_id=chat_id,
        limit=limit,
        before=before,
    )
    return ChatMessageListResponse(
        messages=[_message(message) for message in page.items],
        next_cursor=created_cursor(page.next_before),
    )


def _event(stored: StoredRunEvent) -> ChatEventResponse:
    return ChatEventResponse(
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


@router.get(
    "/{chat_id}/events",
    operation_id="listChatEvents",
    response_model=ChatEventListResponse,
    responses=problems(404),
)
def list_events(
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    before: EventBefore,
    limit: PageLimit = DEFAULT_PAGE_SIZE,
) -> ChatEventListResponse:
    """The chat's events so far, newest first, one page at a time.

    The events of the chat's turns and of the research runs it started. A screen
    reads the first page for the history it shows, and pages further back for
    older messages; it then streams what comes after the newest event of the
    first page (`streamChatEvents` with `after` that event's `id`).
    """
    page = latest_events_of_chat(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        chat_id=chat_id,
        limit=limit,
        before=before,
    )
    return ChatEventListResponse(
        events=[_event(event) for event in page.items],
        next_cursor=event_cursor(page.next_before),
    )


# One event of the stream, written as OpenAPI 3.2 describes a server-sent event.
_STREAMED_EVENT_SCHEMA = {
    "type": "object",
    "required": ["id", "event", "data"],
    "properties": {
        "id": {"type": "string", "description": "The event's `id`."},
        "event": {"type": "string", "description": "The event's `kind`."},
        "data": {
            "type": "string",
            "contentMediaType": "application/json",
            "contentSchema": {"$ref": "#/components/schemas/ChatEventResponse"},
        },
        "retry": {"type": "integer", "minimum": 0},
    },
}

EventCursor = Annotated[int, Query(ge=0, le=HIGHEST_EVENT_ID)]
EventCursorHeader = Annotated[int, Header(ge=0, le=HIGHEST_EVENT_ID)]


@router.get(
    "/{chat_id}/events/stream",
    operation_id="streamChatEvents",
    status_code=200,
    response_class=EventStreamResponse,
    responses={
        200: {
            "description": (
                "Server-sent events, oldest first. Each event is `id: <id>`, `event: <kind>` and"
                " `data: <ChatEventResponse as JSON>`; a `: keep-alive` comment is sent when"
                " nothing happened for a while, and the stream ends after a few minutes with"
                " a `retry:` hint sent at its start, so the client reconnects with"
                " `Last-Event-ID`."
            ),
            "content": {EVENT_STREAM_MEDIA_TYPE: {"itemSchema": _STREAMED_EVENT_SCHEMA}},
        },
        # 403 and 503 too: the workspace router declares them in this route's media type.
        **json_problems(403, 404, 503),
    },
)
def stream_events(
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
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
    """The chat's events as they are stored, from after a cursor, as server-sent events.

    `after` (or the `Last-Event-ID` header, which wins) is the `id` of the last
    event the client has; left out, the stream starts at the chat's first event.
    404 before anything is streamed when the chat is not found, and 503
    `too_many_streams` when the server has as many streams open as it keeps (the
    client tries again after a while). The stream ends when the chat is removed,
    and after a few minutes in any case.
    """
    require_chat(session, workspace_id=workspace_id, project_id=project_id, chat_id=chat_id)
    place = open_streams.take()

    def read_after(cursor: int | None) -> list[StreamedEvent]:
        try:
            with transaction(sessions) as reading:
                events = events_of_chat(
                    reading,
                    workspace_id=workspace_id,
                    project_id=project_id,
                    chat_id=chat_id,
                    after=cursor,
                    limit=STREAM_READ_SIZE,
                )
        except (ProjectNotFound, ChatNotFound) as e:
            raise StreamEnded from e
        except DatabaseError as e:
            # The client reconnects after the stream's retry hint, and is answered 503 if the
            # database is still unavailable then.
            logger.warning("chat %s: events could not be read: %s", chat_id, e)
            raise StreamEnded from e
        return [
            StreamedEvent(
                id=event.id, event=event.kind, data=json_line(_event(event).model_dump(mode="json"))
            )
            for event in events
        ]

    return EventStreamResponse(
        event_stream(
            read_after,
            after=last_event_id if last_event_id is not None else after,
            timing=timing,
            disconnected=request.is_disconnected,
        ),
        place=place,
    )


@router.post(
    "/{chat_id}/messages",
    operation_id="sendChatMessage",
    status_code=201,
    response_model=ChatTurnResponse,
    responses=problems(404, 409),
)
def send(
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    body: ChatMessageRequest,
    session: DbSession,
    sessions: Sessions,
    workspace_id: CurrentWorkspaceId,
    model: Annotated[ChatModel, Depends(chat_model)],
    start_workflow: StartWorkflow,
    tracing: ProcessTracing,
) -> ChatTurnResponse:
    """Answer one operator message in the chat, and store the exchange.

    201 with the stored question and answer. 409 `chat_busy` while another turn
    of the chat is running, and `model_key_not_set`, `credential_unreadable` or
    `stored_setting_invalid` when the turn's model cannot be resolved. 503
    `chat_unanswered` when the agent gave no answer within its limits and
    `model_unavailable` when the provider could not answer; the turn is
    recorded as failed, no message is stored, and sending the message again
    starts a new turn. A turn may start a research run when the operator asks for
    research (the request action `start_research`), and says so in the answer;
    a run whose workflow could not be started is recorded as failed and the
    answer says it could not be started.
    """
    with tracing.traced():
        exchange = ask_project(
            sessions,
            PostgresChatReads(session, workspace_id, project_id),
            requests=PostgresChatRequests(
                sessions=sessions,
                start_workflow=start_workflow,
                workspace_id=workspace_id,
                project_id=project_id,
                chat_id=chat_id,
            ),
            workspace_id=workspace_id,
            project_id=project_id,
            chat_id=chat_id,
            message=body.message,
            model=model,
        )
    return ChatTurnResponse(
        question=_message(exchange.question),
        answer=_message(exchange.answer),
        model=exchange.model,
        cost_usd=exchange.cost_usd,
    )


@router.post(
    "/{chat_id}/read",
    operation_id="markChatRead",
    response_model=ChatThreadResponse,
    responses=problems(404),
    dependencies=[Depends(no_request_body)],
)
def mark_read(
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
) -> ChatThreadResponse:
    """Record that the operator has read the chat up to now; it is no longer unread.

    The request takes no body; one is a 422.
    """
    return _thread(
        mark_chat_read(session, workspace_id=workspace_id, project_id=project_id, chat_id=chat_id)
    )
