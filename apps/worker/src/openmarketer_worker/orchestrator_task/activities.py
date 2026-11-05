"""The three activities the hooks of ``OrchestratorTask`` call: a task starts, ends, or failed.

``start_task`` answers with the limits of the task's graph and the chat that asked for the task,
which the workflow tells about each step that ends while the task goes on.

They hold no rule of their own: how a task changes state is core's (``db/tasks.py``). What they
add is what an activity owes the engine: every failure is raised with a type that says whether
another attempt is worth its cost and with a message that may be stored and shown, and each can
run twice without doing its work twice, because a transition to where a task already is changes
nothing. ``record_end`` stores the task's last event (``finished``, with its outcome) once its
graph's last step stored the report; ``record_failure`` records the task failed with what its
own model calls cost, which it reads from the task's last checkpoint (the thread is forgotten
only after this hook), and its last event (``failed``, with the reason). Both answer with the
chat that asked for the task, so the workflow can start that chat's follow-up turn.

None plans or runs anything: the steps of the graph, the confirmation and the runs of its steps
are the agent runtime's activities, with what ``task.py`` hands them, and a task that is over
is forgotten by the runtime's ``forget_run``, whichever way it ended.

Dependencies are handed in when the worker starts; nothing here reads the environment. The
original error of a failure is logged, not attached: Temporal stores a failure with its causes.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.run_events import append_events
from openmarketer_core.db.session import transaction
from openmarketer_core.db.tasks import (
    TaskFinished,
    TaskNotFound,
    mark_task_failed,
    mark_task_started,
    orchestrator_task,
)
from openmarketer_core.graph_checkpoints.postgres import AttemptInProgress
from openmarketer_core.orchestrator.task.workflow_contract import TASK_RUN_KIND, TaskRunInput
from openmarketer_core.run_events import FAILED, FINISHED, RunEvent
from openmarketer_worker.agent_runtime.activity_support import worded_failures
from openmarketer_worker.agent_runtime.vocabulary import ATTEMPT_STILL_RUNNING
from openmarketer_worker.orchestrator_task import steps
from openmarketer_worker.orchestrator_task.policy import TaskPolicy

logger = logging.getLogger(__name__)


class TaskActivities:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        policy: TaskPolicy,
        *,
        spent: Callable[[TaskRunInput], Decimal],
    ) -> None:
        """``spent`` reads what a task's own model calls cost from its last checkpoint."""
        self._sessions = sessions
        self._policy = policy
        self._spent = spent

    @activity.defn(name=steps.START_TASK)
    def start_task(self, run: TaskRunInput) -> steps.TaskStarted:
        """Mark the task as picked up; say how this worker limits the steps of its graph, and
        which chat asked for the task."""
        with worded_failures(steps.TASK_TEXTS, subject=subject_of(run.run_id)):
            try:
                with transaction(self._sessions) as session:
                    task = mark_task_started(session, **scope_of(run))
            except TaskNotFound as e:
                # The request that created the task may not have committed it yet.
                raise failure(steps.TASK_NOT_FOUND_YET, str(e)) from None
            except TaskFinished as e:
                raise failure(steps.TASK_UNAVAILABLE, str(e)) from None
        return steps.TaskStarted(policy=self._policy, chat_id=task.chat_thread_id)

    @activity.defn(name=steps.RECORD_END)
    def record_end(self, end: steps.TaskEnd) -> steps.TaskEndRecorded:
        """Record the task's last event, ``finished``; answer with the chat that asked for it.

        A task that does not exist for this workspace and project records nothing and has no
        chat to tell.
        """
        run = end.run
        with worded_failures(steps.TASK_TEXTS, subject=subject_of(run.run_id)):
            try:
                with transaction(self._sessions) as session:
                    task = orchestrator_task(session, **scope_of(run))
                    self._append(session, run, RunEvent(FINISHED, {"outcome": end.outcome}))
            except (TaskNotFound, ProjectNotFound) as e:
                logger.info("end of task %s not recorded: %s", run.run_id, e)
                return steps.TaskEndRecorded(chat_id=None)
        return steps.TaskEndRecorded(chat_id=task.chat_thread_id)

    @activity.defn(name=steps.RECORD_FAILURE)
    def record_failure(self, end: steps.TaskEnd) -> steps.TaskEndRecorded:
        """Mark the task failed with the reason and what its model calls cost; its last event.

        Nothing is recorded for a task that succeeded or does not exist, and it has no chat to
        tell then. A task that failed before keeps its first reason.
        """
        run = end.run
        reason = end.reason or steps.TASK_TEXTS.stopped_unexpectedly
        with worded_failures(steps.TASK_TEXTS, subject=subject_of(run.run_id)):
            try:
                spent = self._spent(run)
            except AttemptInProgress:
                # An attempt Temporal gave up on still holds the thread; it ends within its
                # time limit, and the failure is recorded with what it counted then.
                raise failure(ATTEMPT_STILL_RUNNING, steps.EARLIER_ATTEMPT_RUNNING) from None
            try:
                with transaction(self._sessions) as session:
                    task = mark_task_failed(session, **scope_of(run), error=reason, cost_usd=spent)
                    self._append(session, run, RunEvent(FAILED, {"reason": reason}))
            except (TaskNotFound, TaskFinished, ProjectNotFound) as e:
                logger.info("failure of task %s not recorded: %s", run.run_id, e)
                return steps.TaskEndRecorded(chat_id=None)
        return steps.TaskEndRecorded(chat_id=task.chat_thread_id)

    def _append(self, session: Session, run: TaskRunInput, event: RunEvent) -> None:
        append_events(
            session,
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            run_kind=TASK_RUN_KIND,
            run_id=run.run_id,
            events=[event],
        )


def scope_of(run: TaskRunInput) -> dict[str, uuid.UUID]:
    """The task as the scoped functions of ``db.tasks`` take it."""
    return {
        "workspace_id": run.workspace_id,
        "project_id": run.project_id,
        "task_id": run.run_id,
    }


def subject_of(task_id: uuid.UUID) -> str:
    """How the log names the task's work."""
    return f"task {task_id}"


def failure(kind: str, message: str) -> ApplicationError:
    """The failure of ``kind``, which is not tried again when it cannot pass."""
    return ApplicationError(message, type=kind, non_retryable=kind in steps.NOT_RETRIED)
