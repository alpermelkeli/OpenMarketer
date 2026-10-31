"""One turn of a chat from its start to its outcome: what every kind of turn does around the agent.

A turn of the chat agent, whether it answers the operator's message (``ask.py``) or follows
up on a research run or a studio run the chat started (``follow_up.py``), goes the same way:

1. One transaction starts the turn (``db/chat_turns.start_turn``: ``ChatBusy`` while another
   turn of the chat runs), reads the chat's conversation and records ``turn_started``. It is
   committed before the model is called, so a screen reading the chat from another
   connection sees the turn running and the chat busy.
2. The agent answers, recording what it does through ``PostgresTurnEvents``, one short
   transaction per event, so the screen sees each tool as it is called.
3. One transaction records that the turn answered (``finish_turn``, first), stores what the
   turn's kind stores, and records ``turn_answered`` with the stored answer's id. Finishing
   first means a turn that was given up meanwhile (``ChatTurnFinished``) stores nothing.
4. A turn that fails anywhere in 2 or 3 stores no message: it is marked failed with one of
   the fixed sentences of ``rules.failure_reason`` and records ``turn_failed`` with it, and
   the error is raised again. Recording the failure is itself best effort: when the database
   cannot take it, the failure is logged and the turn's own error is the one raised; the
   turn's row then stays running until the next turn of the chat gives it up.

None of the turn's own writes holds a transaction open while the model is called, and none
holds the workspace lock then. The reads the agent makes are the caller's (``ChatReads``):
the API's request session and the follow-up activity's reading session each stay open, in a
transaction that only reads, for the whole turn. This module holds no rule of a turn's kind:
what the agent is given and what is stored are the caller's.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.chat import ChatMessage, chat_history
from openmarketer_core.db.chat_turns import ChatTurnFinished, fail_turn, finish_turn, start_turn
from openmarketer_core.db.models import ChatTurnTrigger
from openmarketer_core.db.run_events import append_events
from openmarketer_core.db.session import DatabaseError, transaction
from openmarketer_core.orchestrator.chat_agent.definitions import CHAT_TURN_KIND
from openmarketer_core.orchestrator.chat_agent.rules import ChatTurn, ChatTurnError, failure_reason
from openmarketer_core.orchestrator.events import TurnEvents
from openmarketer_core.orchestrator.events_postgres import PostgresTurnEvents
from openmarketer_core.run_events import TURN_ANSWERED, TURN_FAILED, TURN_STARTED, RunEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnOf:
    """The chat a turn is taken in, and why it is taken."""

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    chat_id: uuid.UUID
    trigger: ChatTurnTrigger
    research_run_id: uuid.UUID | None = None
    studio_run_id: uuid.UUID | None = None


@dataclass(frozen=True)
class StoredAnswer[Stored]:
    """What a turn's kind stored for its answer, and the stored answer message's id."""

    stored: Stored
    answer_id: uuid.UUID


def take_turn[Stored](
    sessions: sessionmaker[Session],
    turn_of: TurnOf,
    *,
    answer: Callable[[Sequence[ChatMessage], TurnEvents], ChatTurn],
    store: Callable[[Session, uuid.UUID, ChatTurn], StoredAnswer[Stored]],
) -> tuple[Stored, ChatTurn]:
    """Take one turn of the chat: start it, have ``answer`` answer it, ``store`` the answer.

    ``answer`` is given the chat's stored conversation and where the turn's events go;
    ``store`` is given the transaction that finishes the turn and the turn's id. Raises
    ``ChatBusy`` (nothing started) while another turn of the chat runs, ``ProjectNotFound``
    and ``ChatNotFound`` for a chat the workspace does not have, ``ChatTurnError`` when the
    turn was given up before its answer was stored, and whatever ``answer`` or ``store``
    raised, after the turn is recorded as failed.
    """
    turn_id, history = _start(sessions, turn_of)
    try:
        events = PostgresTurnEvents(sessions, turn_of.workspace_id, turn_of.project_id, turn_id)
        turn = answer(history, events)
        return _store(sessions, turn_of, turn_id, turn, store), turn
    except Exception as error:
        _record_failure(sessions, turn_of, turn_id, failure_reason(error))
        raise


def _start(sessions: sessionmaker[Session], turn_of: TurnOf) -> tuple[uuid.UUID, list[ChatMessage]]:
    with transaction(sessions) as session:
        turn = start_turn(
            session,
            workspace_id=turn_of.workspace_id,
            project_id=turn_of.project_id,
            chat_id=turn_of.chat_id,
            trigger=turn_of.trigger,
            research_run_id=turn_of.research_run_id,
            studio_run_id=turn_of.studio_run_id,
        )
        history = chat_history(
            session,
            workspace_id=turn_of.workspace_id,
            project_id=turn_of.project_id,
            chat_id=turn_of.chat_id,
        )
        _append(
            session, turn_of, turn.id, RunEvent(TURN_STARTED, {"trigger": turn_of.trigger.value})
        )
        return turn.id, list(history)


def _store[Stored](
    sessions: sessionmaker[Session],
    turn_of: TurnOf,
    turn_id: uuid.UUID,
    turn: ChatTurn,
    store: Callable[[Session, uuid.UUID, ChatTurn], StoredAnswer[Stored]],
) -> Stored:
    with transaction(sessions) as session:
        try:
            finish_turn(
                session,
                workspace_id=turn_of.workspace_id,
                project_id=turn_of.project_id,
                chat_id=turn_of.chat_id,
                turn_id=turn_id,
            )
        except ChatTurnFinished as e:
            raise ChatTurnError("the turn was given up before its answer was stored") from e
        stored = store(session, turn_id, turn)
        answered = RunEvent(TURN_ANSWERED, {"message_id": str(stored.answer_id)})
        _append(session, turn_of, turn_id, answered)
        return stored.stored


def _record_failure(
    sessions: sessionmaker[Session], turn_of: TurnOf, turn_id: uuid.UUID, reason: str
) -> None:
    try:
        with transaction(sessions) as session:
            fail_turn(
                session,
                workspace_id=turn_of.workspace_id,
                project_id=turn_of.project_id,
                chat_id=turn_of.chat_id,
                turn_id=turn_id,
                error=reason,
            )
            _append(session, turn_of, turn_id, RunEvent(TURN_FAILED, {"reason": reason}))
    except DatabaseError as e:
        logger.warning("chat turn %s failed and could not be recorded as failed: %s", turn_id, e)


def _append(session: Session, turn_of: TurnOf, turn_id: uuid.UUID, event: RunEvent) -> None:
    append_events(
        session,
        workspace_id=turn_of.workspace_id,
        project_id=turn_of.project_id,
        run_kind=CHAT_TURN_KIND,
        run_id=turn_id,
        events=[event],
    )
