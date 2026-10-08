"""How long each step of an analysis may take and how often it is tried again.

One place for the numbers, so the workflow holds none. The policy of the
analysis itself is configuration (``Settings``); the worker hands it to the
workflow through the first activity, because workflow code cannot read the
environment. This module imports nothing with a side effect: the workflow
sandbox loads it.

Why these defaults:

- An attempt at an analysis clones a repository and makes up to 40 model
  calls; the runs measured so far took a few minutes. Thirty minutes ends an
  attempt that hangs without cutting a slow model short.
- Every attempt can spend the whole model budget of a run again, so an
  analysis is tried twice at most: one retry covers a worker that died or a
  model provider that was briefly unavailable, and a failure that repeats is
  reported instead of paid for a third time.
- A rate limit (HTTP 429) lasts tens of seconds, so the retry waits a minute.
- A heartbeat every 20 seconds with a timeout of one minute lets the workflow
  notice a dead worker within a minute instead of after the half hour.
- Marking a run started and recording its failure are single database writes.
  They get short attempts and more of them; recording the failure keeps trying
  for an hour, because a run nobody marks finished blocks its project.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AnalysisPolicy:
    """Limits of the activity that analyses the repository and stores the result."""

    attempt_timeout_seconds: float = 30 * 60
    heartbeat_timeout_seconds: float = 60
    max_attempts: int = 2
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
