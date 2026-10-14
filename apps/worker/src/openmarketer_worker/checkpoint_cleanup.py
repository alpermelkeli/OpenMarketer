"""Removing left-over checkpoints while the worker serves: at start-up, then at an interval.

The checkpoint tables are shared by every graph the worker runs, so the
schedule belongs to the worker and not to one agent. A run's checkpoints are
forgotten by the activity that ends the run. This is for what that misses: a
process that died in between, an attempt that still wrote after its run was
recorded as over, and a run that never ends because its workflow was
terminated or lost.

This module keeps the schedule and knows no rule. Each kind of run has rules
for which of its checkpoints may go, in core, touching only the threads of its
kind. Every agent gets one without writing it: ``stale_threads_of`` wraps core's
rule for any kind, which removes the threads nothing wrote for longer than a run
of the kind can take, and the agent's registration adds it for the agent's kind
(``agent_worker.py``). An agent may hand over a stricter rule of its own as well,
as the analyzer does. ``main.py`` passes on the ones of every agent it serves.
Every round runs each of them in a transaction of its own. One that fails is logged
and tried again the next round: it does not keep the others from running and
never stops the worker or a run. With none given there is nothing to do.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.checkpoint_cleanup import remove_stale_checkpoints
from openmarketer_core.db.session import DatabaseError, transaction

logger = logging.getLogger(__name__)

# Checkpoints hold what a graph was working on, which is not to stay once nothing will
# continue it. A rule is one cheap statement that usually finds nothing; ten minutes bounds
# how long such content stays without a query every few seconds.
CLEANUP_EVERY_SECONDS: float = 10 * 60


@dataclass(frozen=True)
class CheckpointCleanup:
    """One kind of run's way of removing the checkpoints its runs no longer need."""

    runs: str  # what the runs are called in the log, in the plural
    # Removes them within the given session, without committing, and says of how many runs.
    remove: Callable[[Session], int]


def stale_threads_of(kind: str, longest_run: timedelta) -> CheckpointCleanup:
    """Core's rule for any kind of run: the threads of ``kind`` untouched for ``longest_run``."""

    def remove(session: Session) -> int:
        return remove_stale_checkpoints(session, kind=kind, untouched_for=longest_run)

    return CheckpointCleanup(
        runs=f"{kind} runs untouched for longer than a run takes", remove=remove
    )


def remove_leftover_checkpoints(
    sessions: sessionmaker[Session], cleanups: Sequence[CheckpointCleanup]
) -> None:
    """Run every cleanup once, each in its own transaction; a failure of one is only logged."""
    for cleanup in cleanups:
        try:
            with transaction(sessions) as session:
                removed = cleanup.remove(session)
        except DatabaseError as e:
            logger.warning(
                "left-over checkpoints of %s were not removed, trying again later: %s",
                cleanup.runs,
                e,
            )
        except Exception:  # noqa: BLE001  (a defect in one rule must not end the schedule)
            logger.exception(
                "removing left-over checkpoints of %s stopped on an unexpected error",
                cleanup.runs,
            )
        else:
            if removed:
                logger.info("removed the left-over checkpoints of %d %s", removed, cleanup.runs)


async def keep_removing_leftover_checkpoints(
    sessions: sessionmaker[Session],
    cleanups: Sequence[CheckpointCleanup],
    *,
    every_seconds: float,
) -> None:
    """Run the cleanups now and then every ``every_seconds``, until cancelled."""
    while cleanups:
        await asyncio.to_thread(remove_leftover_checkpoints, sessions, cleanups)
        await asyncio.sleep(every_seconds)
