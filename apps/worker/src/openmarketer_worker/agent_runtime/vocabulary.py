"""What the workflow and the activities of the agent runtime agree on, as plain data.

The activity names and inputs, the failure types, the sentences a run is told when a failure
carries no message of its own, the limits an activity runs under, and the bounds on what the
runtime lets into the workflow history. The workflow calls activities by name and the
activities register under the same names, so the workflow side never imports the code that
talks to the database or runs an action.

The workflow sandbox loads this module, so it imports the standard library and the
framework-free types of core's ``agent_runtime`` only: no ``temporalio``, no database, no graph
framework. The annotations of its dataclasses are real types, not text, for the reason given in
core's ``agent_runtime/actions.py``: hence no ``from __future__ import annotations``. It decides
nothing: which failure ends a run and what a text says are used by ``driver.py``,
``workflow_support.py`` and the activities.
"""

from dataclasses import dataclass, fields

from openmarketer_core.agent_runtime.actions import ActionOutcome
from openmarketer_core.agent_runtime.steps import GraphRunRef

RUN_GRAPH_STEP = "run_graph_step"
RUN_ACTION = "run_action"

# The action's own input is core's ``ActionRequest`` and its result an ``ActionOutcome``;
# the step's result is a ``StepOutcome``.


@dataclass(frozen=True)
class StepInput:
    """What ``run_graph_step`` is given: the run, and what came of the action it waited for."""

    run: GraphRunRef
    resume: ActionOutcome | None  # None for the first step of a run


# Failure types of the runtime's activities and of the driver.
DATABASE_UNAVAILABLE = "DatabaseUnavailable"  # the database refused or was unreachable
UNEXPECTED = "Unexpected"  # an error nobody worded; its detail is in the log only
ATTEMPT_STILL_RUNNING = "AttemptStillRunning"  # an attempt given up on has not returned yet
STEP_REFUSED = "StepRefused"  # the run's thread cannot take the step it was asked to
UNKNOWN_ACTION = "UnknownAction"  # the graph asked for an action that is not allowed
INVALID_ACTION_PAYLOAD = "InvalidActionPayload"  # the payload does not fit the action's model
ACTION_RESULT_TOO_LARGE = "ActionResultTooLarge"  # the result is over ``MAX_RESULT_BYTES``
INVALID_ACTION_RESULT = "InvalidActionResult"  # the result is not JSON
TOO_MANY_ACTIONS = "TooManyActions"  # the run asked for more actions than its policy allows

# A failure of these types ends the run instead of being told to the graph: the fault is in the
# agent's graph, routes or handlers and not in the world, so the graph would only ask again.
RUN_ENDING_ACTION_FAILURES = (
    UNKNOWN_ACTION,
    INVALID_ACTION_PAYLOAD,
    ACTION_RESULT_TOO_LARGE,
    INVALID_ACTION_RESULT,
)

# What the runtime lets into the workflow history, which is stored for as long as the namespace
# keeps it and shown in Temporal's UI. A larger output is stored elsewhere and referenced.
# The size of what an action returns, as the data converter encodes its outcome:
MAX_RESULT_BYTES = 32 * 1024
# The size of what a step returns, encoded: the action it asks for, or the output it finishes with.
MAX_STEP_BYTES = 32 * 1024
# The most of an action's failure that the graph is told: it goes into the model's context.
MAX_ERROR_CHARACTERS = 500

# Each wait between two attempts is this many times the one before it.
RETRY_BACKOFF = 2.0

# What the runtime's own step activity says when another attempt holds the run's thread.
EARLIER_ATTEMPT_RUNNING = "an earlier attempt at the run is still running"


@dataclass(frozen=True)
class FailureTexts:
    """The sentences told when a failure carries no message that may be shown.

    The helpers that take texts (``worded_failures``, ``reason_shown``) hold none: an agent
    has its own words for its runs ("the campaign stopped unexpectedly") and gives them
    as data. The runtime's own two activities word their failures with ``RUN_TEXTS`` and
    ``ACTION_TEXTS`` below; an agent's own activities use its texts.
    """

    stopped_unexpectedly: str
    timed_out: str
    worker_stopped: str  # the worker stopped sending heartbeats
    cancelled: str
    database_failed: str

    def __post_init__(self) -> None:
        # An outcome that failed must say what went wrong, so no text may be empty.
        for field in fields(self):
            if not getattr(self, field.name):
                raise ValueError(f"the text {field.name!r} must not be empty")


# The words of ``run_graph_step`` for what it cannot word otherwise.
RUN_TEXTS = FailureTexts(
    stopped_unexpectedly="the run stopped unexpectedly",
    timed_out="the run did not finish in time",
    worker_stopped="the worker running the run stopped responding",
    cancelled="the run was cancelled",
    database_failed="the database could not be reached",
)
# The runtime's words for its own step activity, and for an action that failed: what the graph
# is told (``ActionOutcome.error``).
ACTION_TEXTS = FailureTexts(
    stopped_unexpectedly="the action stopped unexpectedly",
    timed_out="the action did not finish in time",
    worker_stopped="the worker running the action stopped responding",
    cancelled="the action was cancelled",
    database_failed="the database could not be reached",
)


@dataclass(frozen=True)
class DatabaseWritePolicy:
    """Limits of an activity that is one short database transaction."""

    attempt_timeout_seconds: float
    retry_after_seconds: float
    max_retry_wait_seconds: float
    max_attempts: int = 0  # 0: no limit on attempts, only on time
    give_up_after_seconds: float | None = None


@dataclass(frozen=True)
class ActionPolicy:
    """Limits of one action. An action is tried again only when it fails in a way that may pass.

    ``max_attempts`` counts the first one and is at least 1: Temporal reads 0 as
    no limit, and an action has a side effect. ``heartbeat_timeout_seconds`` is
    for an action that runs long: a worker that stops heartbeating is noticed
    within it, and the workflow's cancellation reaches the action at a
    heartbeat. The activity heartbeats three times in it, taking the timeout from
    what the workflow scheduled it with, so this is the one place it is set. A
    short action needs none.

    The values are checked when the policy is made, so a policy is a constant of the
    agent's workflow module and never part of a workflow's input: a ``ValueError``
    while Temporal decodes the input fails the workflow task, which is tried again
    for ever.
    """

    attempt_timeout_seconds: float
    max_attempts: int
    retry_after_seconds: float
    heartbeat_timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        _require_positive(
            attempt_timeout_seconds=self.attempt_timeout_seconds,
            max_attempts=self.max_attempts,
            retry_after_seconds=self.retry_after_seconds,
        )
        if self.heartbeat_timeout_seconds is not None:
            _require_positive(heartbeat_timeout_seconds=self.heartbeat_timeout_seconds)


@dataclass(frozen=True)
class GraphRunPolicy:
    """Limits of the activity that takes a step of the graph, and of the run's actions.

    ``max_actions`` bounds how many actions one run may ask for, and with them
    the size of its workflow history. The step heartbeats three times in
    ``heartbeat_timeout_seconds``, as an action does. Like ``ActionPolicy`` it is checked
    when it is made, and is a constant of the agent's workflow module.
    """

    attempt_timeout_seconds: float
    heartbeat_timeout_seconds: float
    max_attempts: int
    retry_after_seconds: float
    max_actions: int = 200

    def __post_init__(self) -> None:
        _require_positive(
            attempt_timeout_seconds=self.attempt_timeout_seconds,
            heartbeat_timeout_seconds=self.heartbeat_timeout_seconds,
            max_attempts=self.max_attempts,
            retry_after_seconds=self.retry_after_seconds,
            max_actions=self.max_actions,
        )


def _require_positive(**limits: float) -> None:
    for name, value in limits.items():
        if value <= 0:
            raise ValueError(f"{name} must be more than 0, not {value}")
