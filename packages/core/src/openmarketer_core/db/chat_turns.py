"""Chat turns: each turn of the orchestrator in a chat thread, from its start to its outcome.

A turn answers the operator's message (trigger ``message``) or follows up on a
research run, a studio run or a writing run the thread started, once that run
has ended (trigger ``research_finished``, ``studio_finished`` or
``writing_finished``, with the run). A thread
takes one turn at a time: while a turn runs, starting another is ``ChatBusy``.
The workspace lock makes two starts run one after the other, so the second sees
the first; the partial
unique index on the running turns of a thread is the last guard, and its
refusal is ``ChatBusy`` too.

A turn moves forwards only: ``running`` to ``answered`` or to ``failed``, with
a sentence worded for the operator. Recording the same outcome again changes
nothing (a failure keeps its first sentence); recording another outcome of a
finished turn is ``ChatTurnFinished``, so a caller that stores the turn's
answer in the same transaction stores nothing for a turn that was given up.

A process that stops in the middle of a turn leaves it ``running``. A running
turn started more than ``TURN_ABANDONED_AFTER`` ago no longer keeps its thread
busy (``running_turn`` does not return it, nor does ``ChatThread.busy`` count
it), and the next ``start_turn`` of the thread marks it failed with
``ABANDONED_TURN_ERROR`` first, and records ``turn_failed`` with that sentence,
so a screen that showed it running sees it end.

A run is followed up on once: ``start_turn`` of a follow-up turn is
``FollowUpAnswered`` when a turn of the chat has answered about the run,
and ``FollowUpGivenUp`` when ``MAX_FOLLOW_UP_TURNS`` turns were started about it
already, each of which may have spent a model's turn. Both are decided after the
workspace lock, so two overlapping attempts cannot both pass. A follow-up that
never got as far as a turn is recorded with ``record_unstarted_follow_up``: a
turn that failed at once, so the chat shows why it heard nothing.

Every function is scoped to a workspace, a project and a chat, takes a session
and never commits. A project the workspace does not have is
``ProjectNotFound``, a chat the project does not have ``ChatNotFound``, a turn
the chat does not have ``ChatTurnNotFound``. This module runs no agent; the
turn's use cases (``orchestrator/``) do, and write the turn's rows here and
its messages in ``db/chat.py``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openmarketer_core.db.chat import (
    TURN_ABANDONED_AFTER,
    ChatTurnNotFound,
    require_chat,
)
from openmarketer_core.db.models import (
    CHAT_TURN_ERROR_MAX_LENGTH,
    ChatTurnRecord,
    ChatTurnStatus,
    ChatTurnTrigger,
    ResearchRunRecord,
    StudioRunRecord,
    WritingRunRecord,
)
from openmarketer_core.db.research_runs import ResearchRunNotFound
from openmarketer_core.db.run_events import append_events
from openmarketer_core.db.studio_runs import StudioRunNotFound
from openmarketer_core.db.workspace_lock import wait_for_other_writes
from openmarketer_core.db.writing_runs import WritingRunNotFound
from openmarketer_core.orchestrator.chat_agent.definitions import CHAT_TURN_KIND
from openmarketer_core.run_events import TURN_FAILED, TURN_STARTED, RunEvent

ABANDONED_TURN_ERROR = "This turn stopped before it could answer. Please ask again."
# Turns in all that a chat may start about one research run that ended: each may cost a
# model's turn, so a follow-up that keeps failing is not tried for ever.
MAX_FOLLOW_UP_TURNS = 3
_RUNNING_INDEX = "uq_chat_turn_thread_id_running"
# The triggers of a follow-up, each with the column that names its run.
_FOLLOW_UPS = {
    ChatTurnTrigger.RESEARCH_FINISHED: ChatTurnRecord.research_run_id,
    ChatTurnTrigger.STUDIO_FINISHED: ChatTurnRecord.studio_run_id,
    ChatTurnTrigger.WRITING_FINISHED: ChatTurnRecord.writing_run_id,
}


class ChatBusy(Exception):
    """A turn of the chat is running; the chat takes the next one when it has ended."""

    def __init__(self, chat_id: uuid.UUID) -> None:
        self.chat_id = chat_id
        super().__init__(f"chat {chat_id} is answering another turn")


class FollowUpAnswered(Exception):
    """A turn of the chat has answered about the run already: it is followed up once."""

    def __init__(self, chat_id: uuid.UUID, run_id: uuid.UUID) -> None:
        super().__init__(f"chat {chat_id} has followed up on run {run_id}")


class FollowUpGivenUp(Exception):
    """The chat has started as many turns about the run as it may."""

    def __init__(self, chat_id: uuid.UUID, run_id: uuid.UUID) -> None:
        super().__init__(
            f"chat {chat_id} has tried {MAX_FOLLOW_UP_TURNS} times to follow up on run {run_id}"
        )


class ChatTurnFinished(Exception):
    """The turn was answered or failed before; its outcome is recorded once."""

    def __init__(self, turn_id: uuid.UUID, status: ChatTurnStatus) -> None:
        super().__init__(f"chat turn {turn_id} has already {status.value}")


@dataclass(frozen=True)
class ChatTurn:
    """One turn of a chat and how far it got."""

    id: uuid.UUID
    project_id: uuid.UUID
    thread_id: uuid.UUID
    trigger: ChatTurnTrigger
    research_run_id: uuid.UUID | None
    studio_run_id: uuid.UUID | None
    writing_run_id: uuid.UUID | None
    status: ChatTurnStatus
    error: str | None
    created_at: datetime
    finished_at: datetime | None


def start_turn(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    trigger: ChatTurnTrigger,
    research_run_id: uuid.UUID | None = None,
    studio_run_id: uuid.UUID | None = None,
    writing_run_id: uuid.UUID | None = None,
) -> ChatTurn:
    """Start a running turn of the chat, unless another one is running.

    ``research_run_id`` is given exactly for a ``research_finished`` turn,
    ``studio_run_id`` exactly for a ``studio_finished`` one and ``writing_run_id``
    exactly for a ``writing_finished`` one; each names a run of the project that
    this chat asked for, and any other run is ``ResearchRunNotFound``,
    ``StudioRunNotFound`` or ``WritingRunNotFound``. A turn left running for longer than
    ``TURN_ABANDONED_AFTER`` is marked failed first, with its ``turn_failed``
    event. Raises ``ChatBusy`` while another turn is running, and, for a
    follow-up, ``FollowUpAnswered`` and ``FollowUpGivenUp`` (see the module).
    Takes the workspace lock like every write of a workspace.
    """
    followed_run = _followed_run(trigger, research_run_id, studio_run_id, writing_run_id)
    wait_for_other_writes(session, workspace_id)
    require_chat(session, workspace_id=workspace_id, project_id=project_id, chat_id=chat_id)
    if followed_run is not None:
        _require_run_of_chat(session, project_id, chat_id, trigger, followed_run)
        _require_follow_up_to_take(session, project_id, chat_id, trigger, followed_run)
    _give_up_abandoned_turn(session, workspace_id, project_id, chat_id)
    if _running_record(session, project_id, chat_id) is not None:
        raise ChatBusy(chat_id)
    record = ChatTurnRecord(
        project_id=project_id,
        thread_id=chat_id,
        trigger=trigger,
        research_run_id=research_run_id,
        studio_run_id=studio_run_id,
        writing_run_id=writing_run_id,
    )
    try:
        with session.begin_nested():
            session.add(record)
            session.flush()
    except IntegrityError as e:
        if _RUNNING_INDEX in str(e.orig):
            raise ChatBusy(chat_id) from e
        raise
    session.refresh(record)
    return _turn(record)


def finish_turn(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    turn_id: uuid.UUID,
) -> ChatTurn:
    """Record that the turn answered. Safe to repeat; refused for a turn that failed."""
    wait_for_other_writes(session, workspace_id)
    require_chat(session, workspace_id=workspace_id, project_id=project_id, chat_id=chat_id)
    record = _turn_to_change(session, project_id, chat_id, turn_id)
    if record.status is ChatTurnStatus.FAILED:
        raise ChatTurnFinished(turn_id, record.status)
    if record.status is ChatTurnStatus.RUNNING:
        _change(session, record, status=ChatTurnStatus.ANSWERED, finished_at=func.now())
    return _turn(record)


def fail_turn(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    turn_id: uuid.UUID,
    error: str,
) -> ChatTurn:
    """Record that the turn ended without an answer, keeping the start of ``error``.

    ``error`` is a sentence worded for the operator. Failing a turn that failed
    before keeps the first sentence; a turn that answered is
    ``ChatTurnFinished``.
    """
    wait_for_other_writes(session, workspace_id)
    require_chat(session, workspace_id=workspace_id, project_id=project_id, chat_id=chat_id)
    record = _turn_to_change(session, project_id, chat_id, turn_id)
    if record.status is ChatTurnStatus.ANSWERED:
        raise ChatTurnFinished(turn_id, record.status)
    if record.status is ChatTurnStatus.RUNNING:
        _fail(session, record, error)
    return _turn(record)


def running_turn(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, chat_id: uuid.UUID
) -> ChatTurn | None:
    """The turn of the chat that is running now, or ``None`` when the chat is free.

    A turn left running for longer than ``TURN_ABANDONED_AFTER`` is not
    returned: it no longer keeps the chat busy.
    """
    require_chat(session, workspace_id=workspace_id, project_id=project_id, chat_id=chat_id)
    record = _running_record(session, project_id, chat_id)
    return _turn(record) if record is not None else None


def record_unstarted_follow_up(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    error: str,
    research_run_id: uuid.UUID | None = None,
    studio_run_id: uuid.UUID | None = None,
    writing_run_id: uuid.UUID | None = None,
) -> ChatTurn | None:
    """Record a follow-up on a run that never got to a turn, as a turn that failed.

    The run is ``research_run_id``, ``studio_run_id`` or ``writing_run_id``, exactly one of
    them.

    ``error`` is a sentence worded for the operator. The turn is stored failed at once,
    with ``turn_started`` and ``turn_failed`` events, so the chat shows why it heard
    nothing. Nothing is recorded, and ``None`` returned, when the chat has started a turn
    about the run already: that turn shows its own outcome. The run must be one this chat
    asked for (``ResearchRunNotFound``, ``StudioRunNotFound`` or ``WritingRunNotFound``
    otherwise). Takes the workspace lock like every write of a workspace.
    """
    named = {
        ChatTurnTrigger.RESEARCH_FINISHED: research_run_id,
        ChatTurnTrigger.STUDIO_FINISHED: studio_run_id,
        ChatTurnTrigger.WRITING_FINISHED: writing_run_id,
    }
    runs = [(trigger, run_id) for trigger, run_id in named.items() if run_id is not None]
    if len(runs) != 1:
        raise ValueError("a follow-up is about exactly one run")
    trigger, run_id = runs[0]
    wait_for_other_writes(session, workspace_id)
    require_chat(session, workspace_id=workspace_id, project_id=project_id, chat_id=chat_id)
    _require_run_of_chat(session, project_id, chat_id, trigger, run_id)
    if _follow_up_turns(session, project_id, chat_id, trigger, run_id) > 0:
        return None
    error = error[:CHAT_TURN_ERROR_MAX_LENGTH]
    record = ChatTurnRecord(
        project_id=project_id,
        thread_id=chat_id,
        trigger=trigger,
        research_run_id=research_run_id,
        studio_run_id=studio_run_id,
        writing_run_id=writing_run_id,
        status=ChatTurnStatus.FAILED,
        error=error,
        finished_at=func.now(),
    )
    session.add(record)
    session.flush()
    session.refresh(record)
    started = RunEvent(TURN_STARTED, {"trigger": trigger.value})
    _record(
        session,
        workspace_id,
        project_id,
        record.id,
        started,
        RunEvent(TURN_FAILED, {"reason": error}),
    )
    return _turn(record)


def _followed_run(
    trigger: ChatTurnTrigger,
    research_run_id: uuid.UUID | None,
    studio_run_id: uuid.UUID | None,
    writing_run_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """The run a turn of ``trigger`` follows up on, checked against the trigger."""
    if (trigger is ChatTurnTrigger.RESEARCH_FINISHED) != (research_run_id is not None):
        raise ValueError("a research run is named exactly for a turn that follows up on one")
    if (trigger is ChatTurnTrigger.STUDIO_FINISHED) != (studio_run_id is not None):
        raise ValueError("a studio run is named exactly for a turn that follows up on one")
    if (trigger is ChatTurnTrigger.WRITING_FINISHED) != (writing_run_id is not None):
        raise ValueError("a writing run is named exactly for a turn that follows up on one")
    return research_run_id or studio_run_id or writing_run_id


def _require_follow_up_to_take(
    session: Session,
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    trigger: ChatTurnTrigger,
    run_id: uuid.UUID,
) -> None:
    answered = (
        _follow_ups_of_run(project_id, chat_id, trigger, run_id)
        .where(ChatTurnRecord.status == ChatTurnStatus.ANSWERED)
        .exists()
    )
    if session.scalar(select(answered)) is True:
        raise FollowUpAnswered(chat_id, run_id)
    if _follow_up_turns(session, project_id, chat_id, trigger, run_id) >= MAX_FOLLOW_UP_TURNS:
        raise FollowUpGivenUp(chat_id, run_id)


def _follow_up_turns(
    session: Session,
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    trigger: ChatTurnTrigger,
    run_id: uuid.UUID,
) -> int:
    counted = select(func.count()).select_from(
        _follow_ups_of_run(project_id, chat_id, trigger, run_id).subquery()
    )
    return session.scalar(counted) or 0


def _follow_ups_of_run(
    project_id: uuid.UUID, chat_id: uuid.UUID, trigger: ChatTurnTrigger, run_id: uuid.UUID
) -> Select[ChatTurnRecord]:
    return _turns_of_chat(project_id, chat_id).where(
        ChatTurnRecord.trigger == trigger, _FOLLOW_UPS[trigger] == run_id
    )


def _record(
    session: Session,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    turn_id: uuid.UUID,
    *events: RunEvent,
) -> None:
    append_events(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        run_kind=CHAT_TURN_KIND,
        run_id=turn_id,
        events=events,
    )


def _require_run_of_chat(
    session: Session,
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    trigger: ChatTurnTrigger,
    run_id: uuid.UUID,
) -> None:
    if trigger is ChatTurnTrigger.WRITING_FINISHED:
        found = session.scalar(
            select(WritingRunRecord.id).where(
                WritingRunRecord.id == run_id,
                WritingRunRecord.project_id == project_id,
                WritingRunRecord.chat_thread_id == chat_id,
            )
        )
        if found is None:
            raise WritingRunNotFound(project_id, run_id)
        return
    if trigger is ChatTurnTrigger.STUDIO_FINISHED:
        found = session.scalar(
            select(StudioRunRecord.id).where(
                StudioRunRecord.id == run_id,
                StudioRunRecord.project_id == project_id,
                StudioRunRecord.chat_thread_id == chat_id,
            )
        )
        if found is None:
            raise StudioRunNotFound(project_id, run_id)
        return
    found = session.scalar(
        select(ResearchRunRecord.id).where(
            ResearchRunRecord.id == run_id,
            ResearchRunRecord.project_id == project_id,
            ResearchRunRecord.chat_thread_id == chat_id,
        )
    )
    if found is None:
        raise ResearchRunNotFound(project_id, run_id)


def _give_up_abandoned_turn(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID, chat_id: uuid.UUID
) -> None:
    abandoned = session.scalar(
        select(ChatTurnRecord)
        .where(
            ChatTurnRecord.thread_id == chat_id,
            ChatTurnRecord.status == ChatTurnStatus.RUNNING,
            ChatTurnRecord.created_at <= func.now() - TURN_ABANDONED_AFTER,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if abandoned is not None:
        _fail(session, abandoned, ABANDONED_TURN_ERROR)
        failed = RunEvent(TURN_FAILED, {"reason": ABANDONED_TURN_ERROR})
        _record(session, workspace_id, project_id, abandoned.id, failed)


def _running_record(
    session: Session, project_id: uuid.UUID, chat_id: uuid.UUID
) -> ChatTurnRecord | None:
    return session.scalar(
        _turns_of_chat(project_id, chat_id).where(
            ChatTurnRecord.status == ChatTurnStatus.RUNNING,
            ChatTurnRecord.created_at > func.now() - TURN_ABANDONED_AFTER,
        )
    )


def _turns_of_chat(project_id: uuid.UUID, chat_id: uuid.UUID) -> Select[ChatTurnRecord]:
    # Every caller found the chat in the workspace's project first (``require_chat``).
    return select(ChatTurnRecord).where(
        ChatTurnRecord.project_id == project_id, ChatTurnRecord.thread_id == chat_id
    )


def _turn_to_change(
    session: Session, project_id: uuid.UUID, chat_id: uuid.UUID, turn_id: uuid.UUID
) -> ChatTurnRecord:
    """The turn, with its row held until the transaction ends."""
    record = session.scalar(
        _turns_of_chat(project_id, chat_id)
        .where(ChatTurnRecord.id == turn_id)
        .with_for_update(of=ChatTurnRecord)
        # A row the session still holds from before it waited may have changed since.
        .execution_options(populate_existing=True)
    )
    if record is None:
        raise ChatTurnNotFound(chat_id, turn_id)
    return record


def _fail(session: Session, record: ChatTurnRecord, error: str) -> None:
    _change(
        session,
        record,
        status=ChatTurnStatus.FAILED,
        error=error[:CHAT_TURN_ERROR_MAX_LENGTH],
        finished_at=func.now(),
    )


def _change(session: Session, record: ChatTurnRecord, **columns: object) -> None:
    session.execute(update(ChatTurnRecord).where(ChatTurnRecord.id == record.id).values(**columns))
    session.refresh(record)


def _turn(record: ChatTurnRecord) -> ChatTurn:
    return ChatTurn(
        id=record.id,
        project_id=record.project_id,
        thread_id=record.thread_id,
        trigger=record.trigger,
        research_run_id=record.research_run_id,
        studio_run_id=record.studio_run_id,
        writing_run_id=record.writing_run_id,
        status=record.status,
        error=record.error,
        created_at=record.created_at,
        finished_at=record.finished_at,
    )
