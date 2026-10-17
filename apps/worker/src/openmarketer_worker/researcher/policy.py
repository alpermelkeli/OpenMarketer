"""How long each part of a research run may take and how often it is tried again.

One place for the numbers, so the workflow holds none. A run is, in order: the
write that marks it started, the steps of the researcher's graph (planning,
searches, the screening of their results, fetches, readings, one synthesis and
its check, and the report), the searches and fetches the graph asks for as
actions, and, if any of that did not work, the write that records why. Then
its checkpoints are forgotten.

``ResearchPolicy`` limits the steps of the graph and is configuration
(``settings.py`` next to this module reads what an operator changed; the
defaults are here); the worker hands it to the workflow through the first
activity, because workflow code cannot read the environment. The search's and
the fetch's limits are not configuration: they are constants declared on the
actions, in core (``research_agent/definitions.py``, with their reasons), and
are only read here. This module imports nothing with a side effect: the
workflow sandbox loads it.

Why these defaults:

- A step that calls the model does one or two calls (a submission that does not
  fit is asked again); a fetch may render a page with the headless browser.
  Half an hour ends an attempt that hangs without cutting a slow model or a
  slow renderer short.
- An attempt continues from the checkpoints of the one before it: the queries,
  the pages fetched and the readings are in them, so another attempt pays for
  at most the one model call that was in flight. A step is therefore tried five
  times: with the waits below that covers a model provider, the search or the
  browser being unavailable for a quarter of an hour, and a worker restarted
  more than once during one run.
- A rate limit (HTTP 429) lasts tens of seconds, so the first retry waits a
  minute; each later one waits twice as long (1, 2, 4 and 8 minutes).
- A heartbeat timeout of one minute lets the workflow notice a dead worker
  within a minute instead of after the half hour.
- Marking a run started and recording its failure are single database writes.
  They get short attempts and more of them; recording the failure keeps trying
  for an hour, because a run nobody marks finished keeps its checkpoints.
- A run whose workflow is gone (terminated, lost, or given up on recording its
  failure) stays ``running`` and would keep its checkpoints for good, so the
  cleanup takes those of a run that started longer ago than its workflow can
  last (``longest_research_seconds``): every step and action above at their
  limits, and a day in which attempts may sit in the task queue because no
  worker is serving it. A graph of dozens of steps with every one of them
  retried to its limit is a bound for maintenance, not an expected duration.
"""

from __future__ import annotations

from dataclasses import dataclass

from openmarketer_core.research.research_agent import rules
from openmarketer_core.research.research_agent.definitions import (
    FETCH_PAGE,
    SEARCH_WEB,
    agent,
)
from openmarketer_worker.agent_runtime.vocabulary import (
    FORGET_RUN_LIMITS,
    RETRY_BACKOFF,
    DatabaseWritePolicy,
)


@dataclass(frozen=True)
class ResearchPolicy:
    """Limits of each step of the researcher's graph."""

    attempt_timeout_seconds: float = 30 * 60
    heartbeat_timeout_seconds: float = 60
    max_attempts: int = 5
    retry_after_seconds: float = 60


# Five attempts over about eight seconds: long enough for the request that created the
# run to commit it, short enough that a run that does not exist is given up on quickly.
START_RUN = DatabaseWritePolicy(
    attempt_timeout_seconds=30, retry_after_seconds=0.5, max_retry_wait_seconds=10, max_attempts=5
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


def graph_steps(limits: rules.Limits | None = None) -> int:
    """The most steps of the graph one run can take: every step at its limit.

    Per round: the plan, its queries, the screening of their results, its
    fetches and its readings, one synthesis and its check; plus the report at
    the end. The screening is one step whatever its number of decision calls
    (one per batch of results): the step's time limit bounds them all.
    """
    limits = limits or rules.Limits()
    per_round = 3 + limits.max_queries_per_round + 2 * limits.max_pages
    return per_round * limits.max_rounds + 1


def searches_of_a_run(limits: rules.Limits | None = None) -> int:
    """The most searches one run can ask for: every query of every round."""
    limits = limits or rules.Limits()
    return limits.max_queries_per_round * limits.max_rounds


def pages_of_a_run(limits: rules.Limits | None = None) -> int:
    """The most pages one run can fetch: its cap, over every round."""
    return (limits or rules.Limits()).max_pages


def longest_research_seconds(policy: ResearchPolicy, limits: rules.Limits | None = None) -> float:
    """How long after its start a run can still be in progress, with these limits.

    The sum of what the workflow can spend: the write that starts the run, each
    step of the graph and each search and fetch at every attempt's time limit
    with the waits between the attempts, the time ``record_failure`` and the
    forgetting of the checkpoints keep trying, and the allowance for attempts
    waiting on a worker.
    """
    steps = graph_steps(limits)
    step = _attempts_and_waits(
        policy.attempt_timeout_seconds,
        policy.max_attempts,
        policy.retry_after_seconds,
        longest_wait=policy.retry_after_seconds * LONGEST_RETRY_WAIT_IN_FIRST_WAITS,
    )
    search = _attempts_and_waits_of_action(SEARCH_WEB)
    fetch = _attempts_and_waits_of_action(FETCH_PAGE)
    return (
        _attempts_and_waits(
            START_RUN.attempt_timeout_seconds,
            START_RUN.max_attempts,
            START_RUN.retry_after_seconds,
            longest_wait=START_RUN.max_retry_wait_seconds,
        )
        + steps * step
        + searches_of_a_run(limits) * search
        + pages_of_a_run(limits) * fetch
        + _time_to_give_up(RECORD_FAILURE)
        + _time_to_give_up(FORGET_RUN_LIMITS)
        + WORKER_OUTAGE_ALLOWANCE_SECONDS
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
