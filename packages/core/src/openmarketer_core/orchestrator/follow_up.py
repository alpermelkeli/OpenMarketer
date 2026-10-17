"""Following up in a chat on a research run it started, once the run has ended.

When a research run that a chat turn started has ended, succeeded or failed, the
orchestrator speaks again in that chat on its own: ``follow_up_on_research`` takes one turn
(trigger ``research_finished``, with the run) in which the chat agent reads the run's record
and report with its read tools and explains them to the operator. It is the first form of
the design report's "the report is written into the thread": no task, one follow-up turn.

The turn is given the chat's stored conversation and, in the operator's place, an
instruction that code writes from the run's record (``rules.follow_up_instruction``). The
instruction is not stored: only the answer is, as an assistant message with no question
before it (``db/chat.append_answer``). The turn is not offered the request actions, and
could not use one if it called it: it is given no ``ChatRequests``, so a follow-up never
starts another run of its own accord.

The worker calls this from an activity that may run more than once, and two attempts may
overlap. Whether the run was followed up on already is decided when the turn starts, after
the workspace lock (``db/chat_turns.start_turn``): a run a turn of the chat has answered
about is followed up on no more, and a run the chat has started ``MAX_FOLLOW_UP_TURNS``
turns about is given up (``FollowUpGivenUp``), so a follow-up that keeps failing spends at
most that many model turns. A chat that is busy with another turn is ``ChatBusy``, for the
activity to try again later; a run that has not ended yet is ``ResearchRunNotEnded``. The
turn's lifecycle - its rows, its events, how it fails - is ``turn_lifecycle.py``'s.

A follow-up that never got to a turn (the chat stayed busy, the workspace's model could not
be set up, no worker took it) is recorded by ``record_follow_up_not_taken`` as a turn that
failed at once, with a fixed sentence, so the chat shows why it heard nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.chat import ChatMessage, append_answer
from openmarketer_core.db.chat_turns import FollowUpAnswered, record_unstarted_follow_up
from openmarketer_core.db.models import ChatTurnTrigger, ResearchRunStatus
from openmarketer_core.db.research_runs import research_run
from openmarketer_core.db.session import transaction
from openmarketer_core.llm import ChatModel
from openmarketer_core.orchestrator.chat_agent.graph import answer_message
from openmarketer_core.orchestrator.chat_agent.rules import (
    ChatLimits,
    ChatTurn,
    follow_up_instruction,
)
from openmarketer_core.orchestrator.events import TurnEvents
from openmarketer_core.orchestrator.reads import ChatReads
from openmarketer_core.orchestrator.turn_lifecycle import StoredAnswer, TurnOf, take_turn

# The run has an outcome to tell; a queued or running one has none yet.
ENDED = (ResearchRunStatus.SUCCEEDED, ResearchRunStatus.FAILED)
NOT_TAKEN = "The orchestrator could not review the research run: {cause}"


class ResearchRunNotEnded(Exception):
    """The research run has not ended: there is nothing to follow up on yet."""

    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(f"research run {run_id} has not ended")


@dataclass(frozen=True)
class FollowUp:
    """The answer a follow-up turn stored, and what the turn cost to produce it."""

    answer: ChatMessage
    model: str
    cost_usd: float


def follow_up_on_research(
    sessions: sessionmaker[Session],
    reads: ChatReads,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    run_id: uuid.UUID,
    model: ChatModel,
    limits: ChatLimits | None = None,
) -> FollowUp | None:
    """Have the chat agent tell the chat what research run ``run_id`` came to, and store it.

    ``None``, and nothing done, when a turn of the chat has already answered about the run.
    Raises ``ChatBusy`` while another turn of the chat runs, ``FollowUpGivenUp`` when the
    chat has tried as often as it may; ``ProjectNotFound``, ``ChatNotFound`` and
    ``ResearchRunNotFound`` (for a run this chat did not ask for), and
    ``ResearchRunNotEnded``, when there is nothing to follow up on; ``ChatTurnError`` when
    no answer was given within the limits and ``LLMError`` when the model cannot be used,
    after the turn is recorded as failed and nothing is stored.
    """
    with transaction(sessions) as session:
        run = research_run(session, workspace_id=workspace_id, project_id=project_id, run_id=run_id)
    if run.status not in ENDED:
        raise ResearchRunNotEnded(run_id)
    instruction = follow_up_instruction(str(run.id), run.status.value)

    def answer(history: Sequence[ChatMessage], events: TurnEvents) -> ChatTurn:
        conversation: list[dict[str, Any]] = [
            {"role": stored.role.value, "content": stored.content} for stored in history
        ]
        conversation.append({"role": "user", "content": instruction})
        return answer_message(reads, model, conversation, limits=limits, events=events)

    def store(session: Session, turn_id: uuid.UUID, turn: ChatTurn) -> StoredAnswer[ChatMessage]:
        stored = append_answer(
            session,
            workspace_id=workspace_id,
            project_id=project_id,
            chat_id=chat_id,
            turn_id=turn_id,
            answer=turn.answer,
        )
        return StoredAnswer(stored, stored.id)

    turn_of = TurnOf(
        workspace_id, project_id, chat_id, ChatTurnTrigger.RESEARCH_FINISHED, research_run_id=run_id
    )
    try:
        stored, turn = take_turn(sessions, turn_of, answer=answer, store=store)
    except FollowUpAnswered:
        return None
    return FollowUp(answer=stored, model=turn.model, cost_usd=turn.cost_usd)


def record_follow_up_not_taken(
    sessions: sessionmaker[Session],
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    run_id: uuid.UUID,
    cause: str,
) -> bool:
    """Tell the chat that it will not hear about research run ``run_id``, and why.

    ``cause`` is a fixed sentence of the caller's, never what a provider wrote; the chat is
    shown ``NOT_TAKEN`` with it, as a follow-up turn that failed at once. Whether anything was
    recorded: nothing is when the chat started a turn about the run, which shows its own
    outcome. Raises ``ProjectNotFound``, ``ChatNotFound`` and ``ResearchRunNotFound`` when
    there is no chat to tell.
    """
    with transaction(sessions) as session:
        recorded = record_unstarted_follow_up(
            session,
            workspace_id=workspace_id,
            project_id=project_id,
            chat_id=chat_id,
            research_run_id=run_id,
            error=NOT_TAKEN.format(cause=cause),
        )
    return recorded is not None
