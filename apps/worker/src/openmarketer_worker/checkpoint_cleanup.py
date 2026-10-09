"""Removing left-over checkpoints while the worker serves: at start-up, then at an interval.

The checkpoint tables are shared by every graph the worker runs, so their
cleanup belongs to the worker and not to one agent. A run's checkpoints are
forgotten by the activity that ends the run. This is for what that misses: a
process that died in between, an attempt that still wrote after its run was
recorded failed, and a run that never ends because its workflow was terminated
or lost, whose checkpoints go once it started longer ago than a run can last.

This module only keeps the schedule. The rule for what may be removed is
core's (``db/checkpoint_cleanup.py``), and today it knows analysis runs only;
how long a run can last comes from the policy of the workflow that runs it,
handed in by ``main.py``. It removes checkpoints and never changes a run: one
whose workflow is gone stays ``running``. A cleanup that fails is logged and
tried again the next time: it never stops the worker or an analysis.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.checkpoint_cleanup import remove_unneeded_checkpoints
from openmarketer_core.db.session import DatabaseError, transaction

logger = logging.getLogger(__name__)

# Left-over checkpoints hold repository content. The statement is cheap and usually finds
# nothing; ten minutes bounds how long such content stays without a query every few seconds.
CLEANUP_EVERY_SECONDS: float = 10 * 60


def remove_leftover_checkpoints(sessions: sessionmaker[Session], *, longest_run: timedelta) -> None:
    """Remove the checkpoints no run needs, once; a database that fails is only logged.

    ``longest_run`` is how long after its start a run can still be in progress.
    """
    try:
        with transaction(sessions) as session:
            removed = remove_unneeded_checkpoints(session, longest_run=longest_run)
    except DatabaseError as e:
        logger.warning("left-over checkpoints were not removed, trying again later: %s", e)
        return
    if removed:
        logger.info("removed the left-over checkpoints of %d analysis runs", removed)


async def keep_removing_leftover_checkpoints(
    sessions: sessionmaker[Session], *, every_seconds: float, longest_run: timedelta
) -> None:
    """Remove left-over checkpoints now and then every ``every_seconds``, until cancelled."""
    while True:
        try:
            await asyncio.to_thread(remove_leftover_checkpoints, sessions, longest_run=longest_run)
        except Exception:  # noqa: BLE001  (a defect here must not end the schedule)
            logger.exception("removing left-over checkpoints stopped on an unexpected error")
        await asyncio.sleep(every_seconds)
