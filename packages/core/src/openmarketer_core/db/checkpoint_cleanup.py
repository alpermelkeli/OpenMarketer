"""Removing the graph checkpoints that no analysis run needs any more.

A run's checkpoints hold the analyzer's conversation, which is repository
content, and are forgotten when the run ends (``graph_checkpoints/postgres.py``).
That can fail to happen: the process dies between storing the profile and
forgetting, an attempt that was given up on still writes, or the run never
ends at all because its workflow was terminated or lost and nothing records
its failure. This module holds the rule for what may then be removed, and the
one statement that removes it:

    the checkpoints of a thread are kept only while an analysis run with the
    thread's id is unfinished (``queued`` or ``running``) and was started no
    longer ago than a run can take.

So these go:

- the threads of succeeded and failed runs;
- the threads of runs that are still marked ``running`` but started longer
  ago than the caller says a run can be in progress. Nothing is expected to
  continue them. Should an attempt come after all, it starts the analysis
  from the beginning, or continues from what an attempt stored after the
  removal;
- a thread no run has the id of: a run that was deleted, or a thread id that
  is not a run id at all. Analysis runs are the only users of the checkpoint
  tables today, and a thread nobody can name a run for is content nobody will
  continue or forget. Whoever stores another kind of thread there must teach
  this rule about it first.

The age is counted from ``started_at``, not ``created_at``. A run may wait in
the queue for as long as no worker serves it, which has no limit and leaves no
checkpoints; only from the moment a worker picked it up is its duration
something the caller can bound. A run without a start is queued and is kept.

What this does not do: it removes checkpoints only and never changes a run, so
a run whose workflow is gone stays ``running``; it removes rows, which
PostgreSQL keeps in dead tuples until vacuum, in the write-ahead log and in
backups; it does not open the checkpoint store, does not run on a schedule
(the worker does that) and, like the store itself, does not ask whether an
attempt holds the thread. The function takes a session and does not commit.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import Text, cast, column, delete, func, or_, select, table, union
from sqlalchemy.orm import Session

from openmarketer_core.db.analysis_runs import FINISHED
from openmarketer_core.db.checkpoint_schema import CHECKPOINT_TABLES_OF_THREADS
from openmarketer_core.db.models import AnalysisRunRecord


def remove_unneeded_checkpoints(session: Session, *, longest_run: timedelta) -> int:
    """Remove the checkpoints of every thread no run needs; say how many threads.

    ``longest_run`` is how long after its start a run can still be in
    progress; the caller derives it from how it executes runs. An unfinished
    run started longer ago keeps its status and loses its checkpoints.

    This is maintenance of the whole database, not a caller's request: it is
    not scoped to a workspace, reads no run's content and returns only a
    count. One statement, so a run is judged and its rows removed against the
    same state of the database, by the database's clock; a thread written to
    again afterwards is removed by the next call.
    """
    # The library's tables have no model (see ``checkpoint_schema.py``); only this column is used.
    tables = [
        table(name, column("thread_id", Text)) for name in sorted(CHECKPOINT_TABLES_OF_THREADS)
    ]
    threads = union(*(select(t.c.thread_id) for t in tables)).subquery("thread")
    run_in_progress = (
        select(AnalysisRunRecord.id)
        # The run's id as text is the thread's id (``analysis_runs.run_thread_id``). Comparing
        # text never fails on a thread id that is not a UUID; it only finds no run.
        .where(cast(AnalysisRunRecord.id, Text) == threads.c.thread_id)
        .where(AnalysisRunRecord.status.not_in(FINISHED))
        .where(
            or_(
                AnalysisRunRecord.started_at.is_(None),
                AnalysisRunRecord.started_at > func.now() - longest_run,
            )
        )
        .exists()
    )
    removable = select(threads.c.thread_id).where(~run_in_progress).cte("removable")
    removed = [
        delete(t)
        .where(t.c.thread_id.in_(select(removable.c.thread_id)))
        .returning(t.c.thread_id)
        .cte(f"removed_from_{t.name}")
        for t in tables
    ]
    removed_threads = union(*(select(r.c.thread_id) for r in removed)).subquery("removed")
    return session.execute(select(func.count()).select_from(removed_threads)).scalar_one()
