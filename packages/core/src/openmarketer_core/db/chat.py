"""Chat store: the operator's named conversations about a project, and their messages.

A chat is a thread. It is opened empty, named by the operator or left with the
default name, and gathers the exchanges of one conversation; a chat turn writes
the operator's message and the assistant's answer together
(``append_exchange``), in one call, so the conversation always reads back as
whole exchanges. A message is never changed or deleted once stored, and nothing
about how an answer was produced is kept: which model answered and what the
turn cost belong to the turn, not to the chat.

The two messages of an exchange get creation times one after the other - the
answer a moment after the question - because the list and the history order by
creation time: without that, a random identifier would decide whether a
question reads before its own answer. The clock is read after the workspace
lock, so exchanges order as they were written, and the thread's last activity
is the exchange's time.

A thread is unread while a message is newer than the last time the operator
read it (``mark_chat_read``). An empty thread is not unread, whatever
``last_read_at`` holds. A thread still named ``DEFAULT_CHAT_TITLE`` takes its
name from the first line of the operator's first message, cut at a word
boundary; a name the operator gave is never changed here.

Every function is scoped to a workspace and a project, takes a session and
never commits: the caller owns the transaction. Callers get frozen
``ChatThread`` and ``ChatMessage``, never the ORM row. A project the workspace
does not have is ``ProjectNotFound``; a chat the project does not have is
``ChatNotFound``, raised only after the project was found.

This module does not run the chat agent; ``orchestrator/ask.py`` does that and
stores the exchange here. Nothing here talks to a model.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Select, func, select, tuple_
from sqlalchemy.orm import Session

from openmarketer_core.db.models import (
    ChatMessageRecord,
    ChatRole,
    ChatThreadRecord,
    Project,
)
from openmarketer_core.db.pages import CreatedPosition, Page, page_of, rows_to_fetch
from openmarketer_core.db.projects import get_project
from openmarketer_core.db.workspace_lock import wait_for_other_writes

DEFAULT_CHAT_TITLE = "New chat"
AUTOMATIC_TITLE_MAX_LENGTH = 60


class ChatNotFound(Exception):
    """The workspace has no such chat in such a project."""

    def __init__(self, project_id: uuid.UUID, chat_id: uuid.UUID) -> None:
        super().__init__(f"project {project_id} has no chat {chat_id}")


@dataclass(frozen=True)
class ChatThread:
    """One named conversation of a project's chat, as it is now.

    ``unread`` is whether a message was written after the operator last read
    the thread.
    """

    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    created_at: datetime
    last_activity_at: datetime
    last_read_at: datetime | None
    unread: bool


@dataclass(frozen=True)
class ChatMessage:
    """One stored message of a project's chat, as it was written."""

    id: uuid.UUID
    project_id: uuid.UUID
    thread_id: uuid.UUID
    role: ChatRole
    content: str
    created_at: datetime


def create_chat(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    title: str | None = None,
) -> ChatThread:
    """Open a new chat about the project, named ``title`` or with the default name.

    The chat starts empty, and so starts read. Takes the workspace lock like
    every write of a workspace. Raises ``ProjectNotFound`` when the workspace
    has no such project.
    """
    wait_for_other_writes(session, workspace_id)
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    record = ChatThreadRecord(
        project_id=project_id,
        title=title or DEFAULT_CHAT_TITLE,
        last_activity_at=session.execute(select(func.clock_timestamp())).scalar_one(),
    )
    session.add(record)
    session.flush()
    return _thread(record, newest_message_at=None)


def get_chat(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, chat_id: uuid.UUID
) -> ChatThread:
    """The chat as it is now, with whether it has messages the operator has not read.

    Raises ``ProjectNotFound`` when the workspace has no such project and
    ``ChatNotFound`` when the project has no such chat.
    """
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    record = _thread_of_project(session, workspace_id, project_id, chat_id)
    return _thread(record, _newest_message_at(session, chat_id))


def list_chats(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    limit: int,
    before: CreatedPosition | None = None,
) -> Page[ChatThread, CreatedPosition]:
    """The project's chats, most recent activity first.

    At most ``limit`` of them, starting after ``before`` (the ``next_before`` of
    the previous page) or at the most recently active. Raises ``ProjectNotFound``
    when the workspace has no such project.
    """
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    newest = (
        select(func.max(ChatMessageRecord.created_at))
        .where(ChatMessageRecord.thread_id == ChatThreadRecord.id)
        .correlate(ChatThreadRecord)
        .scalar_subquery()
    )
    query = _threads_of_project(workspace_id, project_id)
    if before is not None:
        query = query.where(
            tuple_(ChatThreadRecord.last_activity_at, ChatThreadRecord.id)
            < tuple_(before.created_at, before.id)
        )
    rows = session.execute(
        query.add_columns(newest)
        .order_by(ChatThreadRecord.last_activity_at.desc(), ChatThreadRecord.id.desc())
        .limit(rows_to_fetch(limit))
    )
    threads = [_thread(record, newest_message_at) for record, newest_message_at in rows]
    return page_of(threads, limit, lambda last: CreatedPosition(last.last_activity_at, last.id))


def mark_chat_read(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, chat_id: uuid.UUID
) -> ChatThread:
    """Record that the operator has seen the chat up to now.

    Its ``last_read_at`` becomes the current time, so a message written from now
    on leaves it unread. Takes the workspace lock like every write of a
    workspace. Raises ``ProjectNotFound`` when the workspace has no such project
    and ``ChatNotFound`` when the project has no such chat.
    """
    wait_for_other_writes(session, workspace_id)
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    record = _thread_of_project(session, workspace_id, project_id, chat_id)
    record.last_read_at = session.execute(select(func.clock_timestamp())).scalar_one()
    session.flush()
    return _thread(record, _newest_message_at(session, chat_id))


def list_chat_messages(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    limit: int,
    before: CreatedPosition | None = None,
) -> Page[ChatMessage, CreatedPosition]:
    """The chat's messages, newest first.

    At most ``limit`` of them, starting after ``before`` (the ``next_before`` of the
    previous page) or at the newest. The answer of an exchange is newer than its
    question, so it comes first. Raises ``ProjectNotFound`` when the workspace has
    no such project and ``ChatNotFound`` when the project has no such chat.
    """
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    _thread_of_project(session, workspace_id, project_id, chat_id)
    query = _messages_of_thread(project_id, chat_id)
    if before is not None:
        query = query.where(
            tuple_(ChatMessageRecord.created_at, ChatMessageRecord.id)
            < tuple_(before.created_at, before.id)
        )
    records = session.scalars(
        query.order_by(ChatMessageRecord.created_at.desc(), ChatMessageRecord.id.desc()).limit(
            rows_to_fetch(limit)
        )
    )
    messages = [_message(record) for record in records]
    return page_of(messages, limit, lambda last: CreatedPosition(last.created_at, last.id))


def chat_history(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, chat_id: uuid.UUID
) -> tuple[ChatMessage, ...]:
    """The chat's whole conversation, oldest first, the way a turn reads it.

    A long conversation is shortened by the chat agent before a turn runs
    (``chat_agent.rules.compact_history``), which needs every message, so this is
    not paged. Raises ``ProjectNotFound`` when the workspace has no such project
    and ``ChatNotFound`` when the project has no such chat.
    """
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    _thread_of_project(session, workspace_id, project_id, chat_id)
    records = session.scalars(
        _messages_of_thread(project_id, chat_id).order_by(
            ChatMessageRecord.created_at, ChatMessageRecord.id
        )
    )
    return tuple(_message(record) for record in records)


def append_exchange(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    question: str,
    answer: str,
) -> tuple[ChatMessage, ChatMessage]:
    """Store one exchange of the chat: the operator's message and the assistant's answer.

    Returns the two stored messages, the question first. Both rows are written by
    this call or neither, and the answer's creation time is the question's, a
    moment later. The chat's last activity becomes the exchange's time, and a chat
    still named ``DEFAULT_CHAT_TITLE`` is named after the operator's message when
    that leaves a name. Takes the workspace lock like every write of a workspace.
    Raises ``ProjectNotFound`` when the workspace has no such project and
    ``ChatNotFound`` when the project has no such chat.
    """
    wait_for_other_writes(session, workspace_id)
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    thread = _thread_of_project(session, workspace_id, project_id, chat_id)
    asked_at = session.execute(select(func.clock_timestamp())).scalar_one()
    question_record = ChatMessageRecord(
        project_id=project_id,
        thread_id=chat_id,
        role=ChatRole.USER,
        content=question,
        created_at=asked_at,
    )
    answer_record = ChatMessageRecord(
        project_id=project_id,
        thread_id=chat_id,
        role=ChatRole.ASSISTANT,
        content=answer,
        created_at=asked_at + timedelta(microseconds=1),
    )
    session.add_all([question_record, answer_record])
    thread.last_activity_at = asked_at + timedelta(microseconds=1)
    if thread.title == DEFAULT_CHAT_TITLE:
        automatic = _automatic_title(question)
        if automatic:
            thread.title = automatic
    session.flush()
    return _message(question_record), _message(answer_record)


def _automatic_title(question: str) -> str:
    """The question's first line, its first 60 characters, cut at a word boundary."""
    first_line = (question.splitlines() or [""])[0].strip()
    if len(first_line) <= AUTOMATIC_TITLE_MAX_LENGTH:
        return first_line
    clipped = first_line[:AUTOMATIC_TITLE_MAX_LENGTH]
    boundary = clipped.rfind(" ")
    return clipped[:boundary].rstrip() if boundary > 0 else clipped


def _threads_of_project(workspace_id: uuid.UUID, project_id: uuid.UUID) -> Select[ChatThreadRecord]:
    return (
        select(ChatThreadRecord)
        .join(Project, Project.id == ChatThreadRecord.project_id)
        .where(Project.workspace_id == workspace_id, Project.id == project_id)
    )


def _thread_of_project(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID, chat_id: uuid.UUID
) -> ChatThreadRecord:
    record = session.scalar(
        _threads_of_project(workspace_id, project_id).where(ChatThreadRecord.id == chat_id)
    )
    if record is None:
        raise ChatNotFound(project_id, chat_id)
    return record


def _messages_of_thread(project_id: uuid.UUID, chat_id: uuid.UUID) -> Select[ChatMessageRecord]:
    return select(ChatMessageRecord).where(
        ChatMessageRecord.project_id == project_id, ChatMessageRecord.thread_id == chat_id
    )


def _newest_message_at(session: Session, chat_id: uuid.UUID) -> datetime | None:
    return session.scalar(
        select(func.max(ChatMessageRecord.created_at)).where(ChatMessageRecord.thread_id == chat_id)
    )


def _thread(record: ChatThreadRecord, newest_message_at: datetime | None) -> ChatThread:
    unread = newest_message_at is not None and (
        record.last_read_at is None or newest_message_at > record.last_read_at
    )
    return ChatThread(
        id=record.id,
        project_id=record.project_id,
        title=record.title,
        created_at=record.created_at,
        last_activity_at=record.last_activity_at,
        last_read_at=record.last_read_at,
        unread=unread,
    )


def _message(record: ChatMessageRecord) -> ChatMessage:
    return ChatMessage(
        id=record.id,
        project_id=record.project_id,
        thread_id=record.thread_id,
        role=record.role,
        content=record.content,
        created_at=record.created_at,
    )
