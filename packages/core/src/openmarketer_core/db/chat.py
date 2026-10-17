"""Chat store: the operator's named conversations about a project, and their messages.

A chat is a thread. It is opened empty, named by the operator or left with the
default name, and gathers the exchanges of one conversation; a chat turn writes
the operator's message and the assistant's answer together
(``append_exchange``), in one call, so the conversation always reads back as
whole exchanges. A follow-up turn, which the orchestrator takes on its own when
a research run the thread started has ended, writes an answer with no question
before it (``append_answer``). Every message names the turn that wrote it
(``db/chat_turns.py``), which must be a turn of the same thread. A chat is
deleted whole, with its messages (``delete_chat``); otherwise a stored message
is never changed or deleted. Nothing about how an answer was produced is kept:
which model answered and what the turn cost belong to the turn, not to the
chat.

The two messages of an exchange get creation times one after the other - the
answer a moment after the question - because the list and the history order by
creation time: without that, a random identifier would decide whether a
question reads before its own answer. The clock is read after the workspace
lock, so exchanges order as they were written, and the thread's last activity
is the exchange's time.

A thread is unread while a message is newer than the last time the operator
read it (``mark_chat_read``), and busy while one of its turns is running and
was started less than ``TURN_ABANDONED_AFTER`` ago: a turn running for longer
was left by a process that stopped, and the next turn marks it failed. An
empty thread is not unread, whatever ``last_read_at`` holds. A thread still
named ``DEFAULT_CHAT_TITLE`` takes its name from the first line of the
operator's first message, cut at a word boundary; a name the operator gave is
never changed here.

Every function is scoped to a workspace and a project, takes a session and
never commits: the caller owns the transaction. Callers get frozen
``ChatThread`` and ``ChatMessage``, never the ORM row. A project the workspace
does not have is ``ProjectNotFound``; a chat the project does not have is
``ChatNotFound``, raised only after the project was found, and a turn the chat
does not have is ``ChatTurnNotFound``.

This module does not run the chat agent; ``orchestrator/ask.py`` does that and
stores the exchange here. Nothing here talks to a model.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Exists, Select, delete, exists, func, select, tuple_, update
from sqlalchemy.orm import InstrumentedAttribute, Session

from openmarketer_core.db.models import (
    ChatMessageRecord,
    ChatRole,
    ChatThreadRecord,
    ChatTurnRecord,
    ChatTurnStatus,
    Project,
    ResearchRunRecord,
    RunEventRecord,
    StudioRunRecord,
)
from openmarketer_core.db.pages import CreatedPosition, Page, page_of, rows_to_fetch
from openmarketer_core.db.projects import get_project
from openmarketer_core.db.workspace_lock import wait_for_other_writes
from openmarketer_core.orchestrator.chat_agent.definitions import CHAT_TURN_KIND

DEFAULT_CHAT_TITLE = "New chat"
AUTOMATIC_TITLE_MAX_LENGTH = 60
# A turn still running after this long was left by a process that stopped (a turn is
# bounded by its own limits to well under it): it no longer keeps its thread busy, and the
# next turn of the thread marks it failed (``chat_turns.start_turn``).
TURN_ABANDONED_AFTER = timedelta(minutes=15)


class ChatNotFound(Exception):
    """The workspace has no such chat in such a project."""

    def __init__(self, project_id: uuid.UUID, chat_id: uuid.UUID) -> None:
        super().__init__(f"project {project_id} has no chat {chat_id}")


class ChatTurnNotFound(Exception):
    """The chat has no such turn."""

    def __init__(self, chat_id: uuid.UUID, turn_id: uuid.UUID) -> None:
        super().__init__(f"chat {chat_id} has no turn {turn_id}")


@dataclass(frozen=True)
class ChatThread:
    """One named conversation of a project's chat, as it is now.

    ``unread`` is whether a message was written after the operator last read
    the thread; ``busy`` is whether a turn of it is running now.
    """

    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    created_at: datetime
    last_activity_at: datetime
    last_read_at: datetime | None
    unread: bool
    busy: bool


@dataclass(frozen=True)
class ChatMessage:
    """One stored message of a project's chat, as it was written.

    ``turn_id`` is the turn that wrote it, or ``None`` for a message stored
    before turns were.
    """

    id: uuid.UUID
    project_id: uuid.UUID
    thread_id: uuid.UUID
    turn_id: uuid.UUID | None
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
    return _thread(record, newest_message_at=None, busy=False)


def get_chat(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, chat_id: uuid.UUID
) -> ChatThread:
    """The chat as it is now, with whether it has messages the operator has not read.

    Raises ``ProjectNotFound`` when the workspace has no such project and
    ``ChatNotFound`` when the project has no such chat.
    """
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    record = _thread_of_project(session, workspace_id, project_id, chat_id)
    return _thread(record, _newest_message_at(session, chat_id), _is_busy(session, chat_id))


def require_chat(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, chat_id: uuid.UUID
) -> None:
    """Whether the chat exists in the project, for a store that keeps rows of a chat.

    Raises ``ProjectNotFound`` when the workspace has no such project and
    ``ChatNotFound`` when the project has no such chat; returns nothing else.
    """
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    _thread_of_project(session, workspace_id, project_id, chat_id)


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
        query.add_columns(newest, _turn_running(ChatThreadRecord.id))
        .order_by(ChatThreadRecord.last_activity_at.desc(), ChatThreadRecord.id.desc())
        .limit(rows_to_fetch(limit))
    )
    threads = [_thread(record, newest_message_at, busy) for record, newest_message_at, busy in rows]
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
    return _thread(record, _newest_message_at(session, chat_id), _is_busy(session, chat_id))


def delete_chat(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, chat_id: uuid.UUID
) -> None:
    """Delete the chat with its turns, its messages and its turns' events, for good.

    The research and studio runs the chat asked for are records of the project
    of their own: they stay, and stop naming the chat. Nothing is kept to restore the
    chat from. Takes the workspace lock like every write of a workspace. Raises
    ``ProjectNotFound`` when the workspace has no such project and
    ``ChatNotFound`` when the project has no such chat.
    """
    wait_for_other_writes(session, workspace_id)
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    _thread_of_project(session, workspace_id, project_id, chat_id)
    turns_of_chat = select(ChatTurnRecord.id).where(
        ChatTurnRecord.project_id == project_id, ChatTurnRecord.thread_id == chat_id
    )
    session.execute(
        update(ResearchRunRecord)
        .where(
            ResearchRunRecord.project_id == project_id,
            ResearchRunRecord.chat_thread_id == chat_id,
        )
        .values(chat_thread_id=None)
    )
    session.execute(
        update(StudioRunRecord)
        .where(
            StudioRunRecord.project_id == project_id,
            StudioRunRecord.chat_thread_id == chat_id,
        )
        .values(chat_thread_id=None)
    )
    # The events described the turns' steps to a screen that is gone with the chat.
    session.execute(
        delete(RunEventRecord).where(
            RunEventRecord.project_id == project_id,
            RunEventRecord.run_kind == CHAT_TURN_KIND,
            RunEventRecord.run_id.in_(turns_of_chat),
        )
    )
    session.execute(
        delete(ChatMessageRecord).where(
            ChatMessageRecord.project_id == project_id,
            ChatMessageRecord.thread_id == chat_id,
        )
    )
    session.execute(
        delete(ChatTurnRecord).where(
            ChatTurnRecord.project_id == project_id,
            ChatTurnRecord.thread_id == chat_id,
        )
    )
    session.execute(
        delete(ChatThreadRecord).where(
            ChatThreadRecord.project_id == project_id,
            ChatThreadRecord.id == chat_id,
        )
    )


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
    turn_id: uuid.UUID,
    question: str,
    answer: str,
) -> tuple[ChatMessage, ChatMessage]:
    """Store one exchange of the chat: the operator's message and the assistant's answer.

    Returns the two stored messages, the question first, both written by the
    turn ``turn_id``. Both rows are written by this call or neither, and the
    answer's creation time is the question's, a moment later. The chat's last
    activity becomes the exchange's time, and a chat still named
    ``DEFAULT_CHAT_TITLE`` is named after the operator's message when that leaves
    a name. Takes the workspace lock like every write of a workspace. Raises
    ``ProjectNotFound`` when the workspace has no such project, ``ChatNotFound``
    when the project has no such chat and ``ChatTurnNotFound`` when the chat has
    no such turn.
    """
    wait_for_other_writes(session, workspace_id)
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    thread = _thread_of_project(session, workspace_id, project_id, chat_id)
    _require_turn_of_thread(session, chat_id, turn_id)
    asked_at = session.execute(select(func.clock_timestamp())).scalar_one()
    question_record = ChatMessageRecord(
        project_id=project_id,
        thread_id=chat_id,
        turn_id=turn_id,
        role=ChatRole.USER,
        content=question,
        created_at=asked_at,
    )
    answer_record = ChatMessageRecord(
        project_id=project_id,
        thread_id=chat_id,
        turn_id=turn_id,
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


def append_answer(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    turn_id: uuid.UUID,
    answer: str,
) -> ChatMessage:
    """Store an answer the orchestrator gave on its own, with no question before it.

    A follow-up turn writes it, when a run the thread started has ended. The
    chat's last activity becomes the answer's time, so the chat is unread until
    the operator reads it again; its name is not changed. Takes the workspace
    lock like every write of a workspace. Raises ``ProjectNotFound`` when the
    workspace has no such project, ``ChatNotFound`` when the project has no such
    chat and ``ChatTurnNotFound`` when the chat has no such turn.
    """
    wait_for_other_writes(session, workspace_id)
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    thread = _thread_of_project(session, workspace_id, project_id, chat_id)
    _require_turn_of_thread(session, chat_id, turn_id)
    answered_at = session.execute(select(func.clock_timestamp())).scalar_one()
    record = ChatMessageRecord(
        project_id=project_id,
        thread_id=chat_id,
        turn_id=turn_id,
        role=ChatRole.ASSISTANT,
        content=answer,
        created_at=answered_at,
    )
    session.add(record)
    thread.last_activity_at = answered_at
    session.flush()
    return _message(record)


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


def _require_turn_of_thread(session: Session, chat_id: uuid.UUID, turn_id: uuid.UUID) -> None:
    found = session.scalar(
        select(ChatTurnRecord.id).where(
            ChatTurnRecord.id == turn_id, ChatTurnRecord.thread_id == chat_id
        )
    )
    if found is None:
        raise ChatTurnNotFound(chat_id, turn_id)


def _turn_running(thread_id: InstrumentedAttribute[uuid.UUID] | uuid.UUID) -> Exists:
    """Whether a turn of the thread is running and was not abandoned."""
    return exists().where(
        ChatTurnRecord.thread_id == thread_id,
        ChatTurnRecord.status == ChatTurnStatus.RUNNING,
        ChatTurnRecord.created_at > func.now() - TURN_ABANDONED_AFTER,
    )


def _is_busy(session: Session, chat_id: uuid.UUID) -> bool:
    return session.scalar(select(_turn_running(chat_id))) is True


def _messages_of_thread(project_id: uuid.UUID, chat_id: uuid.UUID) -> Select[ChatMessageRecord]:
    return select(ChatMessageRecord).where(
        ChatMessageRecord.project_id == project_id, ChatMessageRecord.thread_id == chat_id
    )


def _newest_message_at(session: Session, chat_id: uuid.UUID) -> datetime | None:
    return session.scalar(
        select(func.max(ChatMessageRecord.created_at)).where(ChatMessageRecord.thread_id == chat_id)
    )


def _thread(record: ChatThreadRecord, newest_message_at: datetime | None, busy: bool) -> ChatThread:
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
        busy=busy,
    )


def _message(record: ChatMessageRecord) -> ChatMessage:
    return ChatMessage(
        id=record.id,
        project_id=record.project_id,
        thread_id=record.thread_id,
        turn_id=record.turn_id,
        role=record.role,
        content=record.content,
        created_at=record.created_at,
    )
