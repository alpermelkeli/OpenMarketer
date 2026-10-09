"""How long each step of an analysis may take and how often it is tried again.

One place for the numbers, so the workflow holds none. The policy of the
analysis itself is configuration (``settings.py`` next to this module reads
what an operator changed; the defaults are here); the worker hands it to the
workflow through the first activity, because workflow code cannot read the
environment. This module imports nothing with a side effect: the workflow
sandbox loads it.

Why these defaults:

- An attempt at an analysis clones a repository and makes up to 40 model
  calls; the runs measured so far took a few minutes. Thirty minutes ends an
  attempt that hangs without cutting a slow model short.
- An attempt continues from the checkpoints of the one before it as long as
  the repository is at the same commit. The model limits then hold across
  the attempts, and another attempt pays for at most the one model call that
  was in flight. When the repository has a new commit between two attempts,
  the analysis starts over and so do its limits: the limits hold per commit,
  not per run, and a repository that moves between every two attempts can
  cost a run's model budget once per attempt. That needs a failed attempt and
  a push in the minutes before the next one, each time. What every attempt
  does again is clone and scan the repository and run the extractors, which
  costs time (a clone may take up to five minutes) and nothing else. An
  analysis is therefore tried five times: with the waits below that covers a
  model provider or a database that is unavailable for a quarter of an hour,
  and a worker restarted more than once during one run. It is not tried for
  longer, because an unfinished run blocks its project and a defect that
  repeats should be reported, not cloned for.
- A rate limit (HTTP 429) lasts tens of seconds, so the first retry waits a
  minute; each later one waits twice as long (1, 2, 4 and 8 minutes). The same
  waits serve an attempt that was given up on while its thread still runs
  (after a time limit, or when its worker is asked to stop): the next attempt
  is refused until that one has returned, and it returns within minutes.
- A heartbeat every 20 seconds with a timeout of one minute lets the workflow
  notice a dead worker within a minute instead of after the half hour.
- Marking a run started and recording its failure are single database writes.
  They get short attempts and more of them; recording the failure keeps trying
  for an hour, because a run nobody marks finished blocks its project.
- Checkpoints that a finished run left behind hold repository content, so
  every worker removes them when it starts and every ten minutes after that.
  The statement is cheap and usually finds nothing; ten minutes bounds how
  long such content stays without a query every few seconds.
- A run whose workflow is gone (terminated, lost, or given up on recording
  its failure) stays ``running`` and would keep its checkpoints for good. So
  the cleanup also takes those of a run that started longer ago than its
  workflow can last (``longest_analysis_seconds``): every attempt at its time
  limit, the waits between them, the hour of recording a failure, and a day
  in which attempts may sit in the task queue because no worker is serving
  it. That last wait has no limit of its own; a day covers an outage over a
  night, and a run that outlasts it loses only its progress, not its result.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AnalysisPolicy:
    """Limits of the activity that analyses the repository and stores the result."""

    attempt_timeout_seconds: float = 30 * 60
    heartbeat_timeout_seconds: float = 60
    max_attempts: int = 5
    retry_after_seconds: float = 60

    @property
    def heartbeat_every_seconds(self) -> float:
        return self.heartbeat_timeout_seconds / 3


@dataclass(frozen=True)
class DatabaseWritePolicy:
    """Limits of an activity that is one short database transaction."""

    attempt_timeout_seconds: float
    retry_after_seconds: float
    max_retry_wait_seconds: float
    max_attempts: int = 0  # 0: no limit on attempts, only on time
    give_up_after_seconds: float | None = None


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

CHECKPOINT_CLEANUP_EVERY_SECONDS: float = 10 * 60
# Each wait between two attempts at an analysis is this many times the one before it.
ANALYSIS_RETRY_BACKOFF = 2.0
# Temporal's own default: no wait between two attempts is longer than this many first waits.
LONGEST_RETRY_WAIT_IN_FIRST_WAITS = 100
WORKER_OUTAGE_ALLOWANCE_SECONDS: float = 24 * 60 * 60


def longest_analysis_seconds(analysis: AnalysisPolicy) -> float:
    """How long after its start a run can still be in progress, with these limits.

    The sum of what the workflow can spend after ``start_run``: every attempt
    at its time limit, the waits between them, the time ``record_failure``
    keeps trying, and the allowance for attempts waiting on a worker.
    """
    waits_between_attempts = 0.0
    wait = analysis.retry_after_seconds
    for _ in range(analysis.max_attempts - 1):
        waits_between_attempts += wait
        wait = min(
            wait * ANALYSIS_RETRY_BACKOFF,
            analysis.retry_after_seconds * LONGEST_RETRY_WAIT_IN_FIRST_WAITS,
        )
    return (
        analysis.max_attempts * analysis.attempt_timeout_seconds
        + waits_between_attempts
        + (RECORD_FAILURE.give_up_after_seconds or 0)
        + WORKER_OUTAGE_ALLOWANCE_SECONDS
    )
