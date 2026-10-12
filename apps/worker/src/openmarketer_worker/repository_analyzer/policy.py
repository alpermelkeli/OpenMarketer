"""How long each part of an analysis may take and how often it is tried again.

One place for the numbers, so the workflow holds none. A run is, in order: the
write that marks it started, a step of the analyzer's graph that ends by asking
for the clone, the clone, a second step that does the model's work and stores
the profile, and, if any of that did not work, the write that records why. Then
its checkpoints are forgotten.

``AnalysisPolicy`` limits the steps of the graph and is configuration
(``settings.py`` next to this module reads what an operator changed; the
defaults are here); the worker hands it to the workflow through the first
activity, because workflow code cannot read the environment. The clone's limits
are not configuration: they are constants declared on the action, in core
(``analyzer_agent/definitions.py``, with their reasons), and are only read
here. This module imports nothing with a side effect: the workflow sandbox
loads it.

Why these defaults:

- The step that does the model's work makes up to 40 model calls; the runs
  measured so far took a few minutes. Thirty minutes ends an attempt that hangs
  without cutting a slow model short.
- An attempt continues from the checkpoints of the one before it. The commit
  the run reads is in them, so every attempt reads the same commit and the
  model limits hold for the whole run: another attempt pays for at most the
  one model call that was in flight. The repository is not cloned again
  either, unless the attempt runs on a worker whose disk does not have the
  clone, which then fetches that commit. A step is therefore tried five times:
  with the waits below that covers a model provider or a database that is
  unavailable for a quarter of an hour, and a worker restarted more than once
  during one run. It is not tried for longer, because an unfinished run blocks
  its project and a defect that repeats should be reported.
- A rate limit (HTTP 429) lasts tens of seconds, so the first retry waits a
  minute; each later one waits twice as long (1, 2, 4 and 8 minutes). The same
  waits serve an attempt that was given up on while its thread still runs
  (after a time limit, or when its worker is asked to stop): the next attempt
  is refused until that one has returned, and it returns within minutes.
- A heartbeat timeout of one minute lets the workflow notice a dead worker
  within a minute instead of after the half hour.
- Marking a run started and recording its failure are single database writes.
  They get short attempts and more of them; recording the failure keeps trying
  for an hour, because a run nobody marks finished blocks its project.
- A run whose workflow is gone (terminated, lost, or given up on recording
  its failure) stays ``running`` and would keep its checkpoints, and its clone
  on a worker's disk, for good. So the cleanup of the checkpoints and the sweep
  of the clones take those of a run that started longer ago than its workflow
  can last (``longest_analysis_seconds``): every part above at its limits, and
  a day in which attempts may sit in the task queue because no worker is
  serving it. That last wait has no limit of its own; a day covers an outage
  over a night, and a run that outlasts it loses only its progress, not its
  result.
"""

from __future__ import annotations

from dataclasses import dataclass

from openmarketer_core.repository_analysis.analyzer_agent.definitions import (
    CLONE_REPOSITORY,
    agent,
)
from openmarketer_worker.agent_runtime.vocabulary import (
    FORGET_RUN_LIMITS,
    RETRY_BACKOFF,
    DatabaseWritePolicy,
)


@dataclass(frozen=True)
class AnalysisPolicy:
    """Limits of each step of the analyzer's graph."""

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

# The graph pauses once, for its one action: a step up to the clone, and a step after it.
GRAPH_STEPS_OF_A_RUN = 2
# Temporal's own default: no wait between two attempts is longer than this many first waits.
LONGEST_RETRY_WAIT_IN_FIRST_WAITS = 100
WORKER_OUTAGE_ALLOWANCE_SECONDS: float = 24 * 60 * 60


def longest_analysis_seconds(analysis: AnalysisPolicy) -> float:
    """How long after its start a run can still be in progress, with these limits.

    The sum of what the workflow can spend: the write that starts the run, each
    step of the graph and the clone at every attempt's time limit with the waits
    between the attempts, the time ``record_failure`` and the forgetting of the
    checkpoints keep trying, and the allowance for attempts waiting on a worker.
    """
    clone = agent.policies[CLONE_REPOSITORY]
    step = _attempts_and_waits(
        analysis.attempt_timeout_seconds,
        analysis.max_attempts,
        analysis.retry_after_seconds,
        longest_wait=analysis.retry_after_seconds * LONGEST_RETRY_WAIT_IN_FIRST_WAITS,
    )
    return (
        _attempts_and_waits(
            START_RUN.attempt_timeout_seconds,
            START_RUN.max_attempts,
            START_RUN.retry_after_seconds,
            longest_wait=START_RUN.max_retry_wait_seconds,
        )
        + GRAPH_STEPS_OF_A_RUN * step
        + _attempts_and_waits(
            clone.attempt_timeout_seconds,
            clone.max_attempts,
            clone.retry_after_seconds,
            longest_wait=clone.retry_after_seconds * LONGEST_RETRY_WAIT_IN_FIRST_WAITS,
        )
        + _time_to_give_up(RECORD_FAILURE)
        + _time_to_give_up(FORGET_RUN_LIMITS)
        + WORKER_OUTAGE_ALLOWANCE_SECONDS
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
