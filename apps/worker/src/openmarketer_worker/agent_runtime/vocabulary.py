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

import uuid
from dataclasses import dataclass, fields

from openmarketer_core.agent_runtime.actions import (
    ActionOutcome,
    ActionRequest,
    require_positive,
)
from openmarketer_core.agent_runtime.gate import ProposalStatus, RunScope
from openmarketer_core.agent_runtime.steps import GraphRunRef

RUN_GRAPH_STEP = "run_graph_step"
RUN_ACTION = "run_action"
FORGET_RUN = "forget_run"
# The activity that creates the record of a run an action starts, before the workflow starts
# or waits for the child workflow itself.
PREPARE_RUN = "prepare_run"
# The activities of the policy gate, for an agent that has an action a person must approve.
PROPOSE_ACTION = "propose_action"
SETTLE_PROPOSAL = "settle_proposal"

# The action's own input is core's ``ActionRequest`` and its result an ``ActionOutcome``;
# the step's result is a ``StepOutcome``. ``forget_run`` is given the run's ``GraphRunRef``
# and returns nothing. ``prepare_run`` is given the action and the parent's scope and answers
# with the id of the child's run record.


@dataclass(frozen=True)
class StepInput:
    """What ``run_graph_step`` is given: the run, and what came of the action it waited for."""

    run: GraphRunRef
    resume: ActionOutcome | None  # None for the first step of a run


@dataclass(frozen=True)
class PrepareRunInput:
    """What ``prepare_run`` is given: the action that starts another run, and its scope.

    ``scope`` is the parent run's workspace and project, which the child run's record belongs
    to as well. ``request`` carries the action's id, name and payload; the id is the
    idempotency key: the activity creates the child's record under it, so an attempt that is
    tried again finds the same run instead of a second one.
    """

    scope: RunScope
    request: ActionRequest


@dataclass(frozen=True)
class PreparedRun:
    """What ``prepare_run`` answers: the id of the child run the record-creating call made."""

    run_id: uuid.UUID


@dataclass(frozen=True)
class ProposalInput:
    """What ``propose_action`` is given: the action the gate holds, and whose it is.

    ``effect`` is the value of the action's declared ``ActionEffect``, and ``wait_seconds`` how
    long a person has to decide, both from the action's policy.
    """

    scope: RunScope
    run: GraphRunRef
    workflow_id: str
    request: ActionRequest
    effect: str
    wait_seconds: float


@dataclass(frozen=True)
class ProposalHeld:
    """What ``propose_action`` answers: the proposal, and how long is left until its deadline."""

    proposal_id: uuid.UUID
    seconds_left: float


@dataclass(frozen=True)
class SettleInput:
    """What ``settle_proposal`` is given; it answers with the proposal's status, as text."""

    scope: RunScope
    action_id: str
    deadline_passed: bool


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
RUN_OF_ANOTHER_KIND = "RunOfAnotherKind"  # a workflow was started with another agent's run
PROPOSAL_REFUSED = "ProposalRefused"  # a proposal cannot be stored or found for the run's project
ACTION_NOT_APPROVED = "ActionNotApproved"  # an action the gate holds was asked to run unapproved
RUN_REFUSED = "RunRefused"  # an action that starts another run has no project to put it in

# A failure of these types ends the run instead of being told to the graph: the fault is in the
# agent's graph, routes or handlers and not in the world, so the graph would only ask again.
RUN_ENDING_ACTION_FAILURES = (
    UNKNOWN_ACTION,
    INVALID_ACTION_PAYLOAD,
    ACTION_RESULT_TOO_LARGE,
    INVALID_ACTION_RESULT,
    ACTION_NOT_APPROVED,
)

# The failure types of the runtime itself whose message was chosen for whoever reads it: a fixed
# sentence, or one that names the action, the node or the fields at fault and never a value the
# graph, a model or a host wrote. They are what a run is told, and shown, as they are. Every
# other type is shown in the fixed words of the agent's texts, because Temporal and an agent's
# own activities raise errors too (a database driver's, with the host it could not reach; the
# worker's, listing every activity it has), and a message written for a log must not reach a
# run's record, the dashboard or a model. An agent adds the types its own activities and
# handlers word, and never removes one of these.
WORDED_BY_THE_RUNTIME = (
    UNKNOWN_ACTION,
    INVALID_ACTION_PAYLOAD,
    ACTION_RESULT_TOO_LARGE,
    INVALID_ACTION_RESULT,
    TOO_MANY_ACTIONS,
    STEP_REFUSED,
    ATTEMPT_STILL_RUNNING,
    DATABASE_UNAVAILABLE,
    UNEXPECTED,
    PROPOSAL_REFUSED,
    ACTION_NOT_APPROVED,
    RUN_REFUSED,
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

# What the graph is told of an action the gate held that a person did not approve. It goes into
# the model's context, so the agent can revise what it asked for or give up.
NOT_APPROVED_TEXTS = {
    ProposalStatus.REJECTED: "a person rejected the action",
    ProposalStatus.EXPIRED: "nobody approved the action before its deadline",
}

# What the graph is told of a run action whose child did not succeed, whose child workflow id
# was used already, or whose child reported something other than the small result its agent
# reports. The child's own failure stays in its own run's record, in its own words; the parent
# is told one of these fixed sentences and decides what to do, as it does for any failed action.
CHILD_RUN_DID_NOT_FINISH = "the run this action started did not finish"
CHILD_RUN_ALREADY_STARTED = "the run of this action was started already"
CHILD_RUN_UNREADABLE = "the run this action started reported no small outcome"

# While a run waits for a person, it reads the proposal again this often even if no signal came:
# a signal the API could not send, or that reached a worker that stopped, delays a decision by
# this much at most, and never loses it.
APPROVAL_RECHECK_SECONDS = 15 * 60


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


def _require_whole_number(value: int, name: str, *, at_least: int) -> None:
    # Temporal is given a count as a number it can use, never as a float or a bool.
    if isinstance(value, bool) or not isinstance(value, int) or value < at_least:
        raise ValueError(f"{name} must be a whole number of at least {at_least}, not {value!r}")


@dataclass(frozen=True)
class DatabaseWritePolicy:
    """Limits of an activity that is one short database transaction.

    Checked when it is made, as a policy is a constant of the agent's modules or is built from
    settings that were checked when the worker started: a value Temporal cannot use fails the
    workflow task, which is tried again for ever.
    """

    attempt_timeout_seconds: float
    retry_after_seconds: float
    max_retry_wait_seconds: float
    max_attempts: int = 0  # 0: no limit on attempts, only on time
    give_up_after_seconds: float | None = None

    def __post_init__(self) -> None:
        require_positive(
            attempt_timeout_seconds=self.attempt_timeout_seconds,
            retry_after_seconds=self.retry_after_seconds,
            max_retry_wait_seconds=self.max_retry_wait_seconds,
        )
        if self.give_up_after_seconds is not None:
            require_positive(give_up_after_seconds=self.give_up_after_seconds)
        _require_whole_number(self.max_attempts, "max_attempts", at_least=0)


# What ends every run of a graph: forgetting its checkpoints is a few deletes of one thread's
# rows, so an attempt gets the 30 seconds of any single write. ``forget_quietly`` never fails
# for a database that is down, so an attempt is tried again only for a worker that died or a
# connection that hung; the waits (1 second, doubling to a minute) and the hour to give up
# are those of recording a failed analysis. The hour bounds how long a finished run's workflow
# stays open for rows it cannot remove, which is as long as an outage the run's own record rides
# out, and no longer.
FORGET_RUN_LIMITS = DatabaseWritePolicy(
    attempt_timeout_seconds=30,
    retry_after_seconds=1,
    max_retry_wait_seconds=60,
    give_up_after_seconds=60 * 60,
)


# Storing and settling a proposal: single short writes, with the waits of recording a run's
# failure. An hour bounds how long a run keeps trying while the database is down.
PROPOSAL_WRITE_LIMITS = DatabaseWritePolicy(
    attempt_timeout_seconds=30,
    retry_after_seconds=1,
    max_retry_wait_seconds=60,
    give_up_after_seconds=60 * 60,
)


# Creating the record of a run an action starts: one short transaction, tried like recording a
# run's failure, with an hour before it is given up on. A record that cannot be made keeps the
# child from being started; once the attempts are used up, the graph is told so as a failed
# outcome and decides what to do, so the writing has an end.
PREPARE_RUN_LIMITS = DatabaseWritePolicy(
    attempt_timeout_seconds=30,
    retry_after_seconds=1,
    max_retry_wait_seconds=60,
    give_up_after_seconds=60 * 60,
)


@dataclass(frozen=True)
class GraphRunPolicy:
    """Limits of the activity that takes a step of the graph, and of the run's actions.

    ``max_actions`` bounds how many actions one run may ask for, and with them
    the size of its workflow history. The step heartbeats three times in
    ``heartbeat_timeout_seconds``, as an action does. Like core's ``ActionPolicy`` it is
    checked when it is made, and is a constant of the agent's workflow module.
    """

    attempt_timeout_seconds: float
    heartbeat_timeout_seconds: float
    max_attempts: int
    retry_after_seconds: float
    max_actions: int = 200

    def __post_init__(self) -> None:
        require_positive(
            attempt_timeout_seconds=self.attempt_timeout_seconds,
            heartbeat_timeout_seconds=self.heartbeat_timeout_seconds,
            max_attempts=self.max_attempts,
            retry_after_seconds=self.retry_after_seconds,
            max_actions=self.max_actions,
        )
        _require_whole_number(self.max_attempts, "max_attempts", at_least=1)
        _require_whole_number(self.max_actions, "max_actions", at_least=1)
