"""Following up in a chat on a run it started, once the run has ended.

When a research run, a studio run or a writing run that a chat turn started has ended, the
orchestrator speaks again in that chat on its own: ``follow_up_on_run`` takes one turn
(trigger ``research_finished``, ``studio_finished`` or ``writing_finished``, with the run) in
which the chat agent reads the run with its read tools and explains it to the operator: a
research run's findings, what became of a studio run's image (in the Studio library and
waiting to be accepted, handed to a person with the reason, or not made and why), or what
became of a writing run's post (approved and recorded as a dry run, rejected, expired, or not
proposed and why; a writing run ends only once its post is settled). It is the first form of
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
activity to try again later; a run that has not ended yet is ``RunNotEnded``. The turn's
lifecycle - its rows, its events, how it fails - is ``turn_lifecycle.py``'s.

A follow-up that never got to a turn (the chat stayed busy, the workspace's model could not
be set up, no worker took it) is recorded by ``record_follow_up_not_taken`` as a turn that
failed at once, with a fixed sentence, so the chat shows why it heard nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.chat import ChatMessage, append_answer
from openmarketer_core.db.chat_turns import FollowUpAnswered, record_unstarted_follow_up
from openmarketer_core.db.models import (
    ChatTurnTrigger,
    ResearchRunStatus,
    StudioRunStatus,
    WritingRunStatus,
)
from openmarketer_core.db.research_runs import research_run
from openmarketer_core.db.session import transaction
from openmarketer_core.db.studio_runs import studio_run
from openmarketer_core.db.writing_runs import writing_run
from openmarketer_core.llm import ChatModel
from openmarketer_core.orchestrator.chat_agent.graph import answer_message
from openmarketer_core.orchestrator.chat_agent.rules import (
    FOLLOWED_RUN_KINDS,
    ChatLimits,
    ChatTurn,
    follow_up_instruction,
)
from openmarketer_core.orchestrator.events import TurnEvents
from openmarketer_core.orchestrator.reads import ChatReads
from openmarketer_core.orchestrator.turn_lifecycle import StoredAnswer, TurnOf, take_turn
from openmarketer_core.research.workflow_contract import RESEARCH_RUN_KIND
from openmarketer_core.studio.workflow_contract import STUDIO_RUN_KIND
from openmarketer_core.writing.workflow_contract import WRITING_RUN_KIND

# The statuses of a run that has an outcome to tell; a queued or running one has none yet.
RESEARCH_ENDED = (ResearchRunStatus.SUCCEEDED, ResearchRunStatus.FAILED)
STUDIO_ENDED = (StudioRunStatus.SUCCEEDED, StudioRunStatus.NEEDS_REVIEW, StudioRunStatus.FAILED)
WRITING_ENDED = (WritingRunStatus.SUCCEEDED, WritingRunStatus.FAILED)
# The trigger of a follow-up on each kind of run, and how the chat is told it was not taken.
TRIGGERS: Mapping[str, ChatTurnTrigger] = {
    RESEARCH_RUN_KIND: ChatTurnTrigger.RESEARCH_FINISHED,
    STUDIO_RUN_KIND: ChatTurnTrigger.STUDIO_FINISHED,
    WRITING_RUN_KIND: ChatTurnTrigger.WRITING_FINISHED,
}
NOT_TAKEN: Mapping[str, str] = {
    RESEARCH_RUN_KIND: "The orchestrator could not review the research run: {cause}",
    STUDIO_RUN_KIND: "The orchestrator could not review the studio run: {cause}",
    WRITING_RUN_KIND: "The orchestrator could not review the writing run: {cause}",
}


class RunNotEnded(Exception):
    """The run has not ended: there is nothing to follow up on yet."""

    def __init__(self, run_kind: str, run_id: uuid.UUID) -> None:
        super().__init__(f"{run_kind} {run_id} has not ended")


@dataclass(frozen=True)
class FollowUp:
    """The answer a follow-up turn stored, and what the turn cost to produce it."""

    answer: ChatMessage
    model: str
    cost_usd: float


def follow_up_on_run(
    sessions: sessionmaker[Session],
    reads: ChatReads,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    run_kind: str,
    run_id: uuid.UUID,
    model: ChatModel,
    limits: ChatLimits | None = None,
) -> FollowUp | None:
    """Have the chat agent tell the chat what run ``run_id`` of ``run_kind`` came to; store it.

    ``run_kind`` is one of ``FOLLOWED_RUN_KINDS`` (``ValueError`` otherwise, before anything
    is read). ``None``, and nothing done, when a turn of the chat has already
    answered about the run. Raises ``ChatBusy`` while another turn of the chat runs,
    ``FollowUpGivenUp`` when the chat has tried as often as it may; ``ProjectNotFound``,
    ``ChatNotFound``, ``ResearchRunNotFound``, ``StudioRunNotFound`` or ``WritingRunNotFound``
    (for a run this chat did not ask for), and ``RunNotEnded``, when there is nothing to follow
    up on;
    ``ChatTurnError`` when no answer was given within the limits and ``LLMError`` when the
    model cannot be used, after the turn is recorded as failed and nothing is stored.
    """
    trigger = _trigger_of(run_kind)
    status = _ended_status(sessions, workspace_id, project_id, run_kind, run_id)
    instruction = follow_up_instruction(run_kind, str(run_id), status)

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
        workspace_id,
        project_id,
        chat_id,
        trigger,
        research_run_id=run_id if run_kind == RESEARCH_RUN_KIND else None,
        studio_run_id=run_id if run_kind == STUDIO_RUN_KIND else None,
        writing_run_id=run_id if run_kind == WRITING_RUN_KIND else None,
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
    run_kind: str,
    run_id: uuid.UUID,
    cause: str,
) -> bool:
    """Tell the chat that it will not hear about run ``run_id`` of ``run_kind``, and why.

    ``cause`` is a fixed sentence of the caller's, never what a provider wrote; the chat is
    shown the kind's ``NOT_TAKEN`` with it, as a follow-up turn that failed at once. Whether
    anything was recorded: nothing is when the chat started a turn about the run, which shows
    its own outcome. Raises ``ValueError`` for a kind of run a chat does not follow up on, and
    ``ProjectNotFound``, ``ChatNotFound``, ``ResearchRunNotFound``, ``StudioRunNotFound`` and
    ``WritingRunNotFound`` when there is no chat to tell.
    """
    _trigger_of(run_kind)
    with transaction(sessions) as session:
        recorded = record_unstarted_follow_up(
            session,
            workspace_id=workspace_id,
            project_id=project_id,
            chat_id=chat_id,
            research_run_id=run_id if run_kind == RESEARCH_RUN_KIND else None,
            studio_run_id=run_id if run_kind == STUDIO_RUN_KIND else None,
            writing_run_id=run_id if run_kind == WRITING_RUN_KIND else None,
            error=NOT_TAKEN[run_kind].format(cause=cause),
        )
    return recorded is not None


def _trigger_of(run_kind: str) -> ChatTurnTrigger:
    trigger = TRIGGERS.get(run_kind)
    if trigger is None:
        raise ValueError(
            f"a chat follows up on runs of kinds {', '.join(FOLLOWED_RUN_KINDS)}, not {run_kind!r}"
        )
    return trigger


def _ended_status(
    sessions: sessionmaker[Session],
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_kind: str,
    run_id: uuid.UUID,
) -> str:
    """The status the run ended with; ``RunNotEnded`` while it is queued or running."""
    with transaction(sessions) as session:
        if run_kind == RESEARCH_RUN_KIND:
            research = research_run(
                session, workspace_id=workspace_id, project_id=project_id, run_id=run_id
            )
            ended, status = research.status in RESEARCH_ENDED, research.status.value
        elif run_kind == STUDIO_RUN_KIND:
            studio = studio_run(
                session, workspace_id=workspace_id, project_id=project_id, run_id=run_id
            )
            ended, status = studio.status in STUDIO_ENDED, studio.status.value
        else:
            writing = writing_run(
                session, workspace_id=workspace_id, project_id=project_id, run_id=run_id
            )
            ended, status = writing.status in WRITING_ENDED, writing.status.value
    if not ended:
        raise RunNotEnded(run_kind, run_id)
    return status
