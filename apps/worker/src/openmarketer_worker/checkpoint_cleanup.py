"""Removing left-over checkpoints while the worker serves: at start-up, then at an interval.

The checkpoint tables are shared by every graph the worker runs, so the
schedule belongs to the worker and not to one agent. A run's checkpoints are
forgotten by the activity that ends the run. This is for what that misses: a
process that died in between, an attempt that still wrote after its run was
recorded as over, and a run that never ends because its workflow was
terminated or lost.

This module only keeps the schedule and knows no rule. Each kind of run has
its own rule for which of its checkpoints may go, in core, touching only the
threads of its kind; the wiring of the agent that runs them hands it over as a
``CheckpointCleanup``, and ``main.py`` passes on the ones it was given. Every
round runs each of them in a transaction of its own. One that fails is logged
and tried again the next round: it does not keep the others from running and
never stops the worker or a run. With none given there is nothing to do.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

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
