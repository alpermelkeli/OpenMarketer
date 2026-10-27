"""How long each part of a writing run may take, how often it is tried, and how long a run lasts.

One place for the numbers, so the workflow holds none. A run is, in order: the write that marks
it started, the steps of the writer's graph, the draft it stores (``store_draft``), the post it
asks to publish (``publish_post``), which the policy gate holds until a person decides, and
then either the write that settles the post and ends the run, or, if anything did not work,
the write that records why. Then its checkpoints are forgotten.

The graph takes three steps whatever its rounds: an attempt runs node after node until it asks
for an action or ends, so the first step plans, drafts, checks, reviews and revises every round
and asks for ``store_draft``, the second asks for ``publish_post``, and the third records the
result. ``WriterPolicy`` limits each of them and is configuration (``settings.py`` reads what an
operator changed; the defaults are here); the worker hands it to the workflow through the first
activity, because workflow code cannot read the environment. The actions' limits are not
configuration: they are constants declared on the actions in core (``writer_agent/
definitions.py``), and are only read here, with the waits of the policy gate around the action
that publishes. This module imports nothing with a side effect: the workflow sandbox loads it.

Why these defaults:

- The first step makes every model call of the run: a plan and a draft, and for each of the
  three rounds a review and a rewrite, each asked again once when it does not fit: up to
  fourteen calls. Half an hour, the researcher's, ends an attempt that hangs without cutting a
  slow model short.
- An attempt continues from the checkpoints of the one before it, so another attempt pays for
  at most the model call that was in flight. A step is tried five times, the first retry after
  a minute and each later one after twice as long: a rate limit or an outage of a quarter of an
  hour passes, and a worker restarted during a run is ridden out.
- A heartbeat timeout of one minute lets the workflow notice a dead worker within a minute.
- Marking a run started, settling its post and recording its failure are single database
  writes, tried as the researcher's are; settling the post and recording a failure keep trying
  for an hour, because a run nobody marks finished keeps its checkpoints.
- A run whose workflow is gone stays ``running`` and would keep its checkpoints for good, so
  the cleanup takes those of a run that started longer ago than its workflow can last
  (``longest_writing_seconds``): every step and action at its limits, the longest a person may
  take to decide on the post (the gate's three days), and a day in which attempts may sit in
  the task queue because no worker is serving it.
"""

from __future__ import annotations

from dataclasses import dataclass

from openmarketer_core.agent_runtime.gate import may_wait_for_a_person
from openmarketer_core.writing.writer_agent.definitions import PUBLISH_POST, STORE_DRAFT, agent
from openmarketer_worker.agent_runtime.vocabulary import (
    FORGET_RUN_LIMITS,
    PROPOSAL_WRITE_LIMITS,
    RETRY_BACKOFF,
    DatabaseWritePolicy,
)


@dataclass(frozen=True)
class WriterPolicy:
    """Limits of each step of the writer's graph."""

    attempt_timeout_seconds: float = 30 * 60
    heartbeat_timeout_seconds: float = 60
    max_attempts: int = 5
    retry_after_seconds: float = 60


# Five attempts over about eight seconds: long enough for the request that created the run to
# commit it, short enough that a run that does not exist is given up on quickly.
START_RUN = DatabaseWritePolicy(
    attempt_timeout_seconds=30, retry_after_seconds=0.5, max_retry_wait_seconds=10, max_attempts=5
)
RECORD_OUTCOME = DatabaseWritePolicy(
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

ACTIONS = (STORE_DRAFT, PUBLISH_POST)
# One step that ends by asking for each action, and the step that ends the run.
GRAPH_STEPS = len(ACTIONS) + 1


def longest_writing_seconds(policy: WriterPolicy) -> float:
    """How long after its start a run can still be in progress, with these limits.

    The sum of what the workflow can spend: the write that starts the run, each step of the
    graph at every attempt's time limit with the waits between the attempts, each action at
    its limits (the one that publishes with its proposal stored, the longest wait for a person
    and its settlement), the time settling the post or recording the failure and the forgetting
    of the checkpoints keep trying (only one of the two writes happens, the longer is counted),
    and the allowance for attempts waiting on a worker.
    """
    step = _attempts_and_waits(
        policy.attempt_timeout_seconds,
        policy.max_attempts,
        policy.retry_after_seconds,
        longest_wait=policy.retry_after_seconds * LONGEST_RETRY_WAIT_IN_FIRST_WAITS,
    )
    return (
        _attempts_and_waits(
            START_RUN.attempt_timeout_seconds,
            START_RUN.max_attempts,
            START_RUN.retry_after_seconds,
            longest_wait=START_RUN.max_retry_wait_seconds,
        )
        + GRAPH_STEPS * step
        + sum(_longest_action(name) for name in ACTIONS)
        + max(_time_to_give_up(RECORD_OUTCOME), _time_to_give_up(RECORD_FAILURE))
        + _time_to_give_up(FORGET_RUN_LIMITS)
        + WORKER_OUTAGE_ALLOWANCE_SECONDS
    )


def _longest_action(name: str) -> float:
    """One action at its limits; one the gate holds with everything the gate does around it."""
    action = agent.policies[name]
    attempts = _attempts_and_waits(
        action.attempt_timeout_seconds,
        action.max_attempts,
        action.retry_after_seconds,
        longest_wait=action.retry_after_seconds * LONGEST_RETRY_WAIT_IN_FIRST_WAITS,
    )
    if not may_wait_for_a_person(action):
        return attempts
    # Storing the proposal and settling it once a person decided or its deadline passed.
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
        # Not a default: a write with no end would make every run endless, and a bound of 0
        # for it would be a lie the cleanup acts on.
        raise ValueError("a write that ends a run needs a time after which it is given up on")
    return write.give_up_after_seconds
