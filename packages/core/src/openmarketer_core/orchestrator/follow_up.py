"""Following up in a chat on a run it started, once the run has ended.

When a research run, a studio run or a task that a chat turn started has ended, the
orchestrator speaks again in that chat on its own: ``follow_up_on_run`` takes one turn (trigger
``research_finished``, ``studio_finished`` or ``task_finished``, with the run) in which the chat
agent reads the run with its read tools and explains it to the operator: a research run's
findings, what became of a studio run's image (in the Studio library and waiting to be
accepted, handed to a person with the reason, or not made and why), or a task's report, which
code built from its steps' outcomes. It is the design report's "the report is written into the
thread", in one follow-up turn.

A task tells its chat about each of its steps as well: once a step's run has ended and the task
goes on, ``follow_up_on_run`` with the step's id takes one turn (trigger ``task_step_finished``)
in which the chat agent writes the task's progress so far: the earlier steps in a line each,
what this step produced, and what comes next. The step that ends the task has no turn of its
own: the report's turn closes the chain and sums up every step. Each step is followed up on
once, as a run is.

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
    TaskStatus,
)
from openmarketer_core.db.research_runs import research_run
from openmarketer_core.db.session import transaction
from openmarketer_core.db.studio_runs import studio_run
from openmarketer_core.db.tasks import orchestrator_task
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
from openmarketer_core.orchestrator.task.workflow_contract import TASK_RUN_KIND
from openmarketer_core.orchestrator.turn_lifecycle import StoredAnswer, TurnOf, take_turn
from openmarketer_core.research.workflow_contract import RESEARCH_RUN_KIND
from openmarketer_core.studio.workflow_contract import STUDIO_RUN_KIND

# The statuses of a run that has an outcome to tell; a queued or running one has none yet.
RESEARCH_ENDED = (ResearchRunStatus.SUCCEEDED, ResearchRunStatus.FAILED)
STUDIO_ENDED = (StudioRunStatus.SUCCEEDED, StudioRunStatus.NEEDS_REVIEW, StudioRunStatus.FAILED)
TASK_ENDED = (TaskStatus.SUCCEEDED, TaskStatus.FAILED)
# The trigger of a follow-up on each kind of run, and how the chat is told it was not taken.
TRIGGERS: Mapping[str, ChatTurnTrigger] = {
    RESEARCH_RUN_KIND: ChatTurnTrigger.RESEARCH_FINISHED,
    STUDIO_RUN_KIND: ChatTurnTrigger.STUDIO_FINISHED,
    TASK_RUN_KIND: ChatTurnTrigger.TASK_FINISHED,
}
NOT_TAKEN: Mapping[str, str] = {
    RESEARCH_RUN_KIND: "The orchestrator could not review the research run: {cause}",
    STUDIO_RUN_KIND: "The orchestrator could not review the studio run: {cause}",
    TASK_RUN_KIND: "The orchestrator could not report on the task: {cause}",
}
STEP_NOT_TAKEN = "The orchestrator could not write up the task's progress: {cause}"


class RunNotEnded(Exception):
    """The run, or the step of a task, has not ended: there is nothing to follow up on yet."""

    def __init__(self, run_kind: str, run_id: uuid.UUID, step: str | None = None) -> None:
        run = f"{run_kind} {run_id}"
        super().__init__(f"{run if step is None else f'step {step!r} of {run}'} has not ended")


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
    step: str | None = None,
) -> FollowUp | None:
    """Have the chat agent tell the chat what run ``run_id`` of ``run_kind`` came to; store it.

    ``run_kind`` is ``RESEARCH_RUN_KIND``, ``STUDIO_RUN_KIND`` or ``TASK_RUN_KIND``
    (``ValueError`` otherwise, before anything is read). ``step``, for a task only
    (``ValueError`` otherwise), is the id in its plan of a step whose run has ended while the
    task goes on: the turn is about the task's progress up to that step, and ``RunNotEnded``
    while the step's run has no outcome. ``None``, and nothing done, when a turn of the chat
    has already answered about the run (or the step). Raises ``ChatBusy`` while another turn of
    the chat runs, ``FollowUpGivenUp`` when the chat has tried as often as it may;
    ``ProjectNotFound``, ``ChatNotFound``, ``ResearchRunNotFound``, ``StudioRunNotFound`` or
    ``TaskNotFound`` (for a run this chat did not ask for), and ``RunNotEnded``, when there is
    nothing to follow up on; ``ChatTurnError`` when no answer was given within the limits and
    ``LLMError`` when the model cannot be used, after the turn is recorded as failed and nothing
    is stored.
    """
    trigger = _trigger_of(run_kind, step)
    if step is None:
        status = _ended_status(sessions, workspace_id, project_id, run_kind, run_id)
    else:
        status = _step_outcome(sessions, workspace_id, project_id, run_id, step)
    instruction = follow_up_instruction(run_kind, str(run_id), status, step=step)

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
        task_id=run_id if run_kind == TASK_RUN_KIND else None,
        task_step_key=step,
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
    step: str | None = None,
) -> bool:
    """Tell the chat that it will not hear about run ``run_id`` of ``run_kind``, and why.

    ``step`` names the step of a task the follow-up was about, as ``follow_up_on_run`` takes
    it. ``cause`` is a fixed sentence of the caller's, never what a provider wrote; the chat is
    shown the kind's ``NOT_TAKEN`` (``STEP_NOT_TAKEN`` for a step) with it, as a follow-up turn
    that failed at once. Whether
    anything was recorded: nothing is when the chat started a turn about the run, which shows
    its own outcome. Raises ``ValueError`` for a kind of run a chat does not follow up on, and
    ``ProjectNotFound``, ``ChatNotFound``, ``ResearchRunNotFound``, ``StudioRunNotFound`` and
    ``TaskNotFound`` when there is no chat to tell.
    """
    _trigger_of(run_kind, step)
    told = NOT_TAKEN[run_kind] if step is None else STEP_NOT_TAKEN
    with transaction(sessions) as session:
        recorded = record_unstarted_follow_up(
            session,
            workspace_id=workspace_id,
            project_id=project_id,
            chat_id=chat_id,
            research_run_id=run_id if run_kind == RESEARCH_RUN_KIND else None,
            studio_run_id=run_id if run_kind == STUDIO_RUN_KIND else None,
            task_id=run_id if run_kind == TASK_RUN_KIND else None,
            task_step_key=step,
            error=told.format(cause=cause),
        )
    return recorded is not None


def _trigger_of(run_kind: str, step: str | None) -> ChatTurnTrigger:
    trigger = TRIGGERS.get(run_kind)
    if trigger is None:
        raise ValueError(
            f"a chat follows up on runs of kinds {', '.join(FOLLOWED_RUN_KINDS)}, not {run_kind!r}"
        )
    if step is None:
        return trigger
    if run_kind != TASK_RUN_KIND:
        raise ValueError(f"a chat follows up on the steps of a task, not of a {run_kind!r} run")
    return ChatTurnTrigger.TASK_STEP_FINISHED


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
        elif run_kind == TASK_RUN_KIND:
            task = orchestrator_task(
                session, workspace_id=workspace_id, project_id=project_id, task_id=run_id
            )
            ended, status = task.status in TASK_ENDED, task.status.value
        else:
            studio = studio_run(
                session, workspace_id=workspace_id, project_id=project_id, run_id=run_id
            )
            ended, status = studio.status in STUDIO_ENDED, studio.status.value
    if not ended:
        raise RunNotEnded(run_kind, run_id)
    return status


def _step_outcome(
    sessions: sessionmaker[Session],
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    step: str,
) -> str:
    """How the run of the task's step ``step`` ended; ``RunNotEnded`` while it has no outcome."""
    with transaction(sessions) as session:
        task = orchestrator_task(
            session, workspace_id=workspace_id, project_id=project_id, task_id=task_id
        )
    for dispatched in task.steps:
        if dispatched.step_key == step and dispatched.outcome is not None:
            return dispatched.outcome.value
    raise RunNotEnded(TASK_RUN_KIND, task_id, step)
