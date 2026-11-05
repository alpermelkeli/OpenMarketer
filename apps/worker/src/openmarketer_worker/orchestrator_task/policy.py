"""How long each part of a task may take, how often it is tried, and how long a task lasts.

One place for the numbers, so the workflow holds none. A task is, in order: the write that
marks it started, the steps of the task's graph (the plan, its check, each review and the
report), the confirmation of its plan, which the policy gate holds for a person, the runs it
starts and waits for (a research run, a studio run, a writing run, each a child workflow with
its record made first), and either the write that records how it ended or the write that
records why it failed; then the start of its chat's follow-up turn. Then its checkpoints are
forgotten.

``TaskPolicy`` limits each step of the graph; the worker hands it to the workflow through the
first activity, because workflow code cannot read the environment. The confirmation's limits
are constants declared on the action in core (``task_agent/definitions.py``), and the waits of
the runs are each child's own, bounded by its contract (``*_LONGEST_RUN_SECONDS``, which each
child's worker refuses to start beyond). This module imports nothing with a side effect: the
workflow sandbox loads it.

Why these defaults:

- A step makes at most two model calls (the plan or a review, asked again once when it does
  not fit) and no tool calls. Fifteen minutes ends an attempt that hangs without cutting a slow
  model short.
- An attempt continues from the checkpoints of the one before it, so another attempt pays for
  at most the call that was in flight. A step is tried five times, the first retry after a
  minute and each later one after twice as long: a rate limit or an outage of a quarter of an
  hour passes, and a worker restarted during a task is ridden out.
- A heartbeat timeout of one minute lets the workflow notice a dead worker within a minute.
- Marking a task started, recording its end and recording its failure are single database
  writes, tried as the writer's are; the last two keep trying for an hour, because a task
  nobody marks finished keeps its checkpoints.

How long a task lasts (``longest_task_seconds``) is not the sum of everything it may start: a
plan of a dozen studio runs, each allowed sixty days, would make that years. It is bounded by
the deadline instead. The deadline counts from when the task was requested and the review of
every step reads it first, so nothing new starts once it has passed except what one review
before it led to: a change of plan held for a person's confirmation, then the run of one step
(the longest a child may take), then the review that ends the task and its report. So the
bound is the longest deadline a confirmed plan may have (core's ``MAX_TASK_DEADLINE_DAYS``),
then that tail, each part at its limits, and a day in which attempts may sit in the task queue
because no worker is serving it. No timer ends a run a step is waiting for when the deadline
passes: the task hears of the deadline at the next review.
"""

from __future__ import annotations

from dataclasses import dataclass

from openmarketer_core.orchestrator.task.plan import MAX_TASK_DEADLINE_DAYS
from openmarketer_core.orchestrator.task.task_agent.definitions import (
    CONFIRM_PLAN,
    agent,
)
from openmarketer_worker.agent_runtime.vocabulary import (
    FORGET_RUN_LIMITS,
    PREPARE_RUN_LIMITS,
    PROPOSAL_WRITE_LIMITS,
    RETRY_BACKOFF,
    DatabaseWritePolicy,
)


@dataclass(frozen=True)
class TaskPolicy:
    """Limits of each step of the task's graph."""

    attempt_timeout_seconds: float = 15 * 60
    heartbeat_timeout_seconds: float = 60
    max_attempts: int = 5
    retry_after_seconds: float = 60


# Five attempts over about eight seconds: long enough for the request that created the task to
# commit it, short enough that a task that does not exist is given up on quickly.
START_TASK = DatabaseWritePolicy(
    attempt_timeout_seconds=30, retry_after_seconds=0.5, max_retry_wait_seconds=10, max_attempts=5
)
RECORD_END = DatabaseWritePolicy(
    attempt_timeout_seconds=30,
    retry_after_seconds=1,
    max_retry_wait_seconds=60,
    give_up_after_seconds=60 * 60,
)
RECORD_FAILURE = DatabaseWritePolicy(
    attempt_timeout_seconds=30,
    retry_after_seconds=1,
    max_retry_wait_seconds=60,
    give_up_after_seconds=60 * 60,
)

# Temporal's own default: no wait between two attempts is longer than this many first waits.
LONGEST_RETRY_WAIT_IN_FIRST_WAITS = 100
WORKER_OUTAGE_ALLOWANCE_SECONDS: float = 24 * 60 * 60
DAY_SECONDS: float = 24 * 60 * 60
# What a task can still do once its deadline has passed, in steps of its graph: the step that
# ends asking for the confirmation of a change, the one that ends asking for a step's run, the
# review that ends the task, and its report, which may be a step of its own when it is retried.
STEPS_AFTER_THE_DEADLINE = 4


def longest_task_seconds(policy: TaskPolicy) -> float:
    """How long after it was requested a task can still be in progress, with these limits.

    The longest deadline, then what can follow it: the steps that can still run, one
    confirmation with its proposal stored and settled and the longest wait for a person, the
    record of the longest run a step may wait for, that run, the write that ends the task or
    records its failure (the longer is counted), the forgetting of its checkpoints, and the
    allowance for attempts waiting on a worker.
    """
    step = _attempts_and_waits(
        policy.attempt_timeout_seconds,
        policy.max_attempts,
        policy.retry_after_seconds,
        longest_wait=policy.retry_after_seconds * LONGEST_RETRY_WAIT_IN_FIRST_WAITS,
    )
    return (
        MAX_TASK_DEADLINE_DAYS * DAY_SECONDS
        + STEPS_AFTER_THE_DEADLINE * step
        + _longest_confirmation()
        + _time_to_give_up(PREPARE_RUN_LIMITS)
        + longest_child_seconds()
        + max(_time_to_give_up(RECORD_END), _time_to_give_up(RECORD_FAILURE))
        + _time_to_give_up(FORGET_RUN_LIMITS)
        + WORKER_OUTAGE_ALLOWANCE_SECONDS
    )


def longest_child_seconds() -> float:
    """The longest a run a step starts may take, as its contract bounds it."""
    return max(target.longest_seconds for target in agent.run_targets.values())


def _longest_confirmation() -> float:
    """The confirmation at its limits, with the proposal stored, settled and waited for."""
    action = agent.policies[CONFIRM_PLAN]
    attempts = _attempts_and_waits(
        action.attempt_timeout_seconds,
        action.max_attempts,
        action.retry_after_seconds,
        longest_wait=action.retry_after_seconds * LONGEST_RETRY_WAIT_IN_FIRST_WAITS,
    )
    return attempts + 2 * _time_to_give_up(PROPOSAL_WRITE_LIMITS) + action.approval_wait_seconds


def _attempts_and_waits(
    attempt_timeout: float, attempts: int, first_wait: float, *, longest_wait: float
) -> float:
    """Every attempt of an activity at its time limit, and the waits between them."""
    waits = 0.0
    wait = first_wait
    for _ in range(attempts - 1):
        waits += wait
        wait = min(wait * RETRY_BACKOFF, longest_wait)
    return attempts * attempt_timeout + waits


def _time_to_give_up(write: DatabaseWritePolicy) -> float:
    if write.give_up_after_seconds is None:
        # Not a default: a write with no end would make every task endless, and a bound of 0
        # for it would be a lie the cleanup acts on.
        raise ValueError("a write that ends a task needs a time after which it is given up on")
    return write.give_up_after_seconds
