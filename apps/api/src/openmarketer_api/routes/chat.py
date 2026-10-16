"""Chats: the operator's named conversations with the orchestrator about one project.

The operator opens a chat and sends it messages; the chat agent answers each in
this process - it reads the project's stored records and returns one answer, and
it may start a research run when the operator asks for research - and the
exchange is stored under the chat. One route opens a chat, one lists the
project's chats, most recently active first, one reads a chat, one pages a
chat's messages, newest first, one sends a message and one records that the
operator has read a chat. Nothing else changes the project, and no route
presents a chat as approved by anyone: the agent reads and answers and starts
research runs, and approvals, settings and every other kind of run stay on their
own routes.

Unlike an analysis, which a worker runs, a chat turn runs in the API's process,
so this module resolves the turn's model itself: the workspace's choice for the
``orchestrator`` role (``model_choices``) with the provider's key
(``credentials.resolution.model_key``). ``chat_model`` is a dependency, so a
test can hand the route a scripted model instead. The turn's request action
(reaching ``PostgresChatRequests``) is wired from the request's session factory
and the same workflow starter the research route uses.

A turn that fails stores nothing (``orchestrator/ask.py``), so the same message
may be sent again; this module words no failure of the agent's.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from openmarketer_api.chat_requests import PostgresChatRequests
from openmarketer_api.dependencies import (
    DbSession,
    Environment,
    Models,
    Sessions,
    StartWorkflow,
    StoringCipher,
    no_request_body,
)
from openmarketer_api.errors import problems
from openmarketer_api.identity import CurrentWorkspaceId
from openmarketer_api.pagination import DEFAULT_PAGE_SIZE, CreatedBefore, PageLimit, created_cursor
from openmarketer_core.credentials.resolution import model_key
from openmarketer_core.db.chat import (
    ChatMessage,
    ChatThread,
    create_chat,
    get_chat,
    list_chat_messages,
    list_chats,
    mark_chat_read,
)
from openmarketer_core.db.models import ChatRole
from openmarketer_core.llm import ChatModel, RouterChatModel
from openmarketer_core.orchestrator.ask import ask_project
from openmarketer_core.orchestrator.reads_postgres import PostgresChatReads
from openmarketer_core.workspace_settings.model_settings import model_choices

router = APIRouter(prefix="/projects/{project_id}/chats", tags=["chat"])

MAX_TITLE_LENGTH = 200
MAX_MESSAGE_LENGTH = 10_000


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
) -> ChatModel:
    """The model one chat turn calls: the workspace's choice of the role, with its key.

    Raises ``StoredModelChoiceInvalid`` when the workspace's stored choice broke a
    rule, ``ModelKeyNotSet`` when the key of the provider's variable is neither in
    the environment nor stored, and ``CredentialUnreadable`` when a stored key
    cannot be opened.
    """
    chosen = model_choices(session, models, workspace_id=workspace_id).router_for(models)
    key = model_key(session, environ, workspace_id=workspace_id, router=chosen, cipher=cipher)
    return RouterChatModel(router=chosen, api_key=key)


def _thread(stored: ChatThread) -> ChatThreadResponse:
    return ChatThreadResponse(
        id=stored.id,
        project_id=stored.project_id,
        title=stored.title,
        created_at=stored.created_at,
        last_activity_at=stored.last_activity_at,
        last_read_at=stored.last_read_at,
        unread=stored.unread,
    )


def _message(stored: ChatMessage) -> ChatMessageResponse:
    return ChatMessageResponse(
        id=stored.id,
        project_id=stored.project_id,
        chat_id=stored.thread_id,
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
) -> ChatTurnResponse:
    """Answer one operator message in the chat, and store the exchange.

    201 with the stored question and answer. 409 `model_key_not_set`,
    `credential_unreadable` or `stored_setting_invalid` when the turn's model
    cannot be resolved. 503 `chat_unanswered` when the agent gave no answer
    within its limits and `model_unavailable` when the provider could not
    answer; nothing is stored then, and sending the message again starts the
    turn again. A turn may start a research run when the operator asks for
    research (the request action `start_research`), and says so in the answer;
    a run whose workflow could not be started is recorded as failed and the
    answer says it could not be started.
    """
    exchange = ask_project(
        session,
        PostgresChatReads(session, workspace_id, project_id),
        requests=PostgresChatRequests(
            sessions=sessions,
            start_workflow=start_workflow,
            workspace_id=workspace_id,
            project_id=project_id,
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
