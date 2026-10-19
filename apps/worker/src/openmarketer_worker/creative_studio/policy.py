"""How long each part of a studio run may take, how often it is tried, and how long a run lasts.

One place for the numbers, so the workflow holds none. A run is, in order: the write that
marks it started, the steps of the studio's graph (the brief, each round's operations chosen
and asked for, the check, the verifier, the decision and the edit plan, then the end), the
operations the graph asks for as actions (a paid generation or edit, a render or a program in
the sandbox, the reading of a candidate's text), and, if any of that did not work, the write
that records why; then the write that tells the chat that asked for the run how it ended, and
the start of that chat's follow-up turn. Then its checkpoints are forgotten.

``StudioPolicy`` limits the steps of the graph and is configuration (``settings.py`` reads
what an operator changed; the defaults are here); the worker hands it to the workflow through
the first activity, because workflow code cannot read the environment. The actions' limits are
not configuration: they are constants declared on the actions in core
(``studio_agent/definitions.py``, with their reasons), and are only read here, with the
waits of the policy gate around an action that spends. This module imports nothing with a side
effect: the workflow sandbox loads it.

Why these defaults:

- A step that calls a model does a few calls: ``produce`` up to six (its read tools and a
  submission asked again), ``verify`` one or two with an image. Twenty minutes ends an attempt
  that hangs without cutting a slow model short.
- An attempt continues from the checkpoints of the one before it, so another attempt pays for
  at most the model calls of one step. A step is tried four times, the first retry after a
  minute and each later one after twice as long: a rate limit or a provider outage of a few
  minutes passes, and a worker restarted during a run is ridden out.
- A heartbeat timeout of one minute lets the workflow notice a dead worker within a minute.
- Marking a run started and recording its failure are single database writes, tried as the
  researcher's are.
- A run whose workflow is gone stays ``running`` and would keep its checkpoints for good, so
  the cleanup takes those of a run that started longer ago than its workflow can last
  (``longest_studio_seconds``): every step and action at its limits, every paid operation
  waiting the longest a person may take to approve it, and a day in which attempts may sit in
  the task queue because no worker is serving it. That is weeks: a bound for maintenance, not
  an expected duration, since a run with a dozen paid images each waiting days for a person is
  the worst case the budget allows.
"""

from __future__ import annotations

from dataclasses import dataclass

from openmarketer_core.studio.studio_agent import rules
from openmarketer_core.studio.studio_agent.definitions import (
    EDIT_IMAGE,
    GENERATE_IMAGE,
    READ_TEXT,
    RENDER_HTML,
    RUN_PYTHON,
    agent,
)
from openmarketer_worker.agent_runtime.vocabulary import (
    FORGET_RUN_LIMITS,
    PROPOSAL_WRITE_LIMITS,
    RESERVE_SPEND_LIMITS,
    RETRY_BACKOFF,
    SETTLE_SPEND_LIMITS,
    DatabaseWritePolicy,
)


@dataclass(frozen=True)
class StudioPolicy:
    """Limits of each step of the studio's graph."""

    attempt_timeout_seconds: float = 20 * 60
    heartbeat_timeout_seconds: float = 60
    max_attempts: int = 4
    retry_after_seconds: float = 60


# Five attempts over about eight seconds: long enough for the request that created the run to
# commit it, short enough that a run that does not exist is given up on quickly.
START_RUN = DatabaseWritePolicy(
    attempt_timeout_seconds=30, retry_after_seconds=0.5, max_retry_wait_seconds=10, max_attempts=5
)
RECORD_FAILURE = DatabaseWritePolicy(
    attempt_timeout_seconds=30,
    retry_after_seconds=1,
    max_retry_wait_seconds=60,
    give_up_after_seconds=60 * 60,
)
# Telling the chat that asked for the run how it ended is for a screen and a follow-up turn,
# not for the run's record: ten minutes of trying, and the run ends as it did without it.
RECORD_END = DatabaseWritePolicy(
    attempt_timeout_seconds=30,
    retry_after_seconds=1,
    max_retry_wait_seconds=60,
    give_up_after_seconds=10 * 60,
)

# Temporal's own default: no wait between two attempts is longer than this many first waits.
LONGEST_RETRY_WAIT_IN_FIRST_WAITS = 100
WORKER_OUTAGE_ALLOWANCE_SECONDS: float = 24 * 60 * 60

SPENDING_ACTIONS = (GENERATE_IMAGE, EDIT_IMAGE)
OPERATIONS = (GENERATE_IMAGE, EDIT_IMAGE, RENDER_HTML, RUN_PYTHON)


def graph_steps(limits: rules.Limits | None = None) -> int:
    """The most steps of the graph one run can take: every round at its limits.

    Per round: a ``produce`` and an ``operate`` for each operation, the ``produce`` that ends
    the round, the check, the verifier, the decision and the edit plan; plus the brief and the
    end (``finalize`` or ``escalate``).
    """
    limits = limits or rules.Limits()
    per_round = 2 * limits.max_operations_per_round + 5
    return per_round * limits.max_rounds + 2


def operations_of_a_run(limits: rules.Limits | None = None) -> int:
    """The most operations one run can ask for: every operation of every round."""
    limits = limits or rules.Limits()
    return limits.max_operations_per_round * limits.max_rounds


def longest_studio_seconds(policy: StudioPolicy, limits: rules.Limits | None = None) -> float:
    """How long after its start a run can still be in progress, with these limits.

    The sum of what the workflow can spend: the write that starts the run, each step of the
    graph at every attempt's time limit with the waits between the attempts, each operation as
    the longest of the four can take (a paid one with its reservation, its proposal, the
    longest wait for a person and its settlement), the reading of each round's candidate, the
    time ``record_failure``, ``record_end`` and the forgetting of the checkpoints keep trying
    (the follow-up is started, not waited for), and the allowance for attempts waiting on a
    worker.
    """
    limits = limits or rules.Limits()
    step = _attempts_and_waits(
        policy.attempt_timeout_seconds,
        policy.max_attempts,
        policy.retry_after_seconds,
        longest_wait=policy.retry_after_seconds * LONGEST_RETRY_WAIT_IN_FIRST_WAITS,
    )
    operation = max(_longest_operation(name) for name in OPERATIONS)
    return (
        _attempts_and_waits(
            START_RUN.attempt_timeout_seconds,
            START_RUN.max_attempts,
            START_RUN.retry_after_seconds,
            longest_wait=START_RUN.max_retry_wait_seconds,
        )
        + graph_steps(limits) * step
        + operations_of_a_run(limits) * operation
        + limits.max_rounds * _attempts_and_waits_of_action(READ_TEXT)
        + _time_to_give_up(RECORD_FAILURE)
        + _time_to_give_up(RECORD_END)
        + _time_to_give_up(FORGET_RUN_LIMITS)
        + WORKER_OUTAGE_ALLOWANCE_SECONDS
    )


def _longest_operation(name: str) -> float:
    """One operation at its limits; a paid one with everything the gate does around it."""
    action = _attempts_and_waits_of_action(name)
    if name not in SPENDING_ACTIONS:
        return action
    return (
        action
        + _time_to_give_up(RESERVE_SPEND_LIMITS)
        # Storing the proposal and settling it once a person decided.
        + 2 * _time_to_give_up(PROPOSAL_WRITE_LIMITS)
        + agent.policies[name].approval_wait_seconds
        + _time_to_give_up(SETTLE_SPEND_LIMITS)
    )


def _attempts_and_waits_of_action(name: str) -> float:
    action = agent.policies[name]
    return _attempts_and_waits(
        action.attempt_timeout_seconds,
        action.max_attempts,
        action.retry_after_seconds,
        longest_wait=action.retry_after_seconds * LONGEST_RETRY_WAIT_IN_FIRST_WAITS,
    )


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
