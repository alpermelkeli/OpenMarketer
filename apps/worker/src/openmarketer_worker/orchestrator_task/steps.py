"""The activities of ``OrchestratorTask`` that are the task's own: names, inputs and failures.

The workflow calls activities by name and the activities register under the same names, so
the workflow module never imports the code that talks to the database (the workflow sandbox
could not load it). The steps of the task's graph, its confirmation and the runs it starts are
the agent runtime's activities, named there (``agent_runtime.vocabulary``); the three here are
what the workflow's hooks call. Everything here is stored in the workflow history:
identifiers, a status, an amount and short messages only, never a request, a plan or a report.

A task is told why it failed in its own words: ``in_the_tasks_words`` turns the runtime's
sentences for an action or an attempt still running into the task's.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, fields

from openmarketer_core.orchestrator.task.workflow_contract import TaskRunInput
from openmarketer_worker.agent_runtime import vocabulary
from openmarketer_worker.agent_runtime.vocabulary import (
    ACTION_TEXTS,
    WORDED_BY_THE_RUNTIME,
    FailureTexts,
)

START_TASK = "start_task"
# Once a task is over, either way: its last event, and the chat that asked for it, if one did.
RECORD_END = "record_end"
RECORD_FAILURE = "record_failure"

# Failure types of the task's activities, of its handler, of the prepare callables of its runs
# and of a step of its graph. Those in NOT_RETRIED end the task (a step), the run action (a
# prepare) or the hook at once. The ones every agent's activities share (the database, an
# attempt still running, an unexpected error) are the runtime's.
TASK_FAILED = "TaskFailed"  # the plan, the request or the model's answers were refused
TASK_UNAVAILABLE = "TaskUnavailable"  # the task does not exist, or it is already finished
TASK_NOT_FOUND_YET = "TaskNotFoundYet"  # the task may not have been committed yet
STEP_NOT_STARTED = "StepNotStarted"  # a step's run could not be created as it was asked
MODEL_UNAVAILABLE = "ModelUnavailable"  # the provider failed or limited the rate
NOT_RETRIED = (TASK_FAILED, TASK_UNAVAILABLE, STEP_NOT_STARTED)
# Failures of these types carry a message written for the task's reader (and, for a run
# action, for the task's graph): the task's own, and the runtime's.
WORDED_BY_THE_ACTIVITY = (
    TASK_FAILED,
    TASK_UNAVAILABLE,
    TASK_NOT_FOUND_YET,
    STEP_NOT_STARTED,
    MODEL_UNAVAILABLE,
    *WORDED_BY_THE_RUNTIME,
)

# What a task is told when its failure carries no message that may be shown.
TASK_TEXTS = FailureTexts(
    stopped_unexpectedly="the task stopped unexpectedly",
    timed_out="the task did not finish in time",
    worker_stopped="the worker running the task stopped responding",
    cancelled="the task was cancelled",
    database_failed="the database could not be reached",
)
NOT_STORED = "the task finished, but its report could not be stored"
EARLIER_ATTEMPT_RUNNING = "an earlier attempt at the task is still running"
# What a task whose result cannot be read ends with: a defect of its ``finish``.
RESULT_UNREADABLE = "the task finished without an outcome it could record"

# The runtime's sentences that can end a task, each with the task's for it.
_THE_TASKS_WORDS_FOR = {
    **{
        getattr(ACTION_TEXTS, text.name): getattr(TASK_TEXTS, text.name)
        for text in fields(FailureTexts)
    },
    vocabulary.EARLIER_ATTEMPT_RUNNING: EARLIER_ATTEMPT_RUNNING,
}


def in_the_tasks_words(reason: str) -> str:
    """``reason`` as a task is told it: a sentence of the runtime's in the task's."""
    return _THE_TASKS_WORDS_FOR.get(reason, reason)


@dataclass(frozen=True)
class TaskEnd:
    """How a task ended, as its last event says it and the chat that asked for it is told.

    ``outcome`` is core's ``TaskOutcome`` value of a task that ended with its report;
    ``reason`` is why it ended without one, in the words recorded on the task.
    """

    run: TaskRunInput
    outcome: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class TaskEndRecorded:
    """What ``record_end`` and ``record_failure`` answer: the task's chat, or ``None``."""

    chat_id: uuid.UUID | None
