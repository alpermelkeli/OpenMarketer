"""Removing the graph checkpoints that runs no longer need: the analysis runs' rule, and one
for any kind of run.

A run's checkpoints hold the analyzer's conversation, which is repository
content, and are forgotten when the run ends (``graph_checkpoints/postgres.py``).
That can fail to happen: the process dies between storing the profile and
forgetting, an attempt that was given up on still writes, or the run never
ends at all because its workflow was terminated or lost and nothing records
its failure. This module holds the analysis runs' rule for what may then be
removed, and the one statement that removes it.

The checkpoint tables are shared by every graph, and this rule is about one
kind of thread only: those named for an analysis run
(``analysis_runs.run_thread_id``, names that start with
``ANALYSIS_RUN_THREADS``). Among them:

    the checkpoints of a thread are kept only while the analysis run it is
    named for is unfinished (``queued`` or ``running``) and was started no
    longer ago than a run can take.

So these go:

- the threads of succeeded and failed runs;
- the threads of runs that are still marked ``running`` but started longer
  ago than the caller says a run can be in progress. Nothing is expected to
  continue them. Should an attempt come after all, it starts the analysis
  from the beginning, or continues from what an attempt stored after the
  removal;
- a thread named for an analysis run that does not exist: the run was
  deleted, or what follows the prefix is not the id of a run at all.

A thread of any other name is not this rule's and is left alone, whatever its
age. Another kind of run names its threads with its own prefix and brings its
own rule: one written for its runs like this one, or ``remove_stale_checkpoints``,
which needs no table of runs. A thread of a kind no rule knows, such as one whose
prefix was given up, is nobody's to remove and its rows stay; the worker refuses an
agent whose kind has no rule, so that is only ever a kind no agent runs any more.

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

from sqlalchemy import DateTime, Text, cast, column, delete, func, or_, select, table, union
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from openmarketer_core.agent_runtime.threads import validate_kind
from openmarketer_core.db.analysis_runs import ANALYSIS_RUN_THREADS, FINISHED
from openmarketer_core.db.checkpoint_schema import CHECKPOINT_TABLES_OF_THREADS
from openmarketer_core.db.models import (
    AnalysisRunRecord,
    ResearchRunRecord,
    StudioRunRecord,
    WritingRunRecord,
)
from openmarketer_core.db.research_runs import FINISHED as RESEARCH_FINISHED
from openmarketer_core.db.research_runs import RESEARCH_RUN_THREADS
from openmarketer_core.db.studio_runs import FINISHED as STUDIO_FINISHED
from openmarketer_core.db.studio_runs import STUDIO_RUN_THREADS
from openmarketer_core.db.writing_runs import FINISHED as WRITING_FINISHED
from openmarketer_core.db.writing_runs import WRITING_RUN_THREADS


def remove_checkpoints_of_analysis_runs(session: Session, *, longest_run: timedelta) -> int:
    """Remove the checkpoints analysis runs no longer need; say how many threads.

    ``longest_run`` is how long after its start a run can still be in
    progress; the caller derives it from how it executes runs. An unfinished
    run started longer ago keeps its status and loses its checkpoints. Threads
    that are not named for an analysis run are not looked at.

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
    threads = union(
        *(
            select(t.c.thread_id).where(
                t.c.thread_id.startswith(ANALYSIS_RUN_THREADS, autoescape=True)
            )
            for t in tables
        )
    ).subquery("thread")
    run_in_progress = (
        select(AnalysisRunRecord.id)
        # The name is compared as text, the way ``run_thread_id`` builds it. A name whose
        # rest is not a run id therefore never fails; it only finds no run.
        .where(
            func.concat(ANALYSIS_RUN_THREADS, cast(AnalysisRunRecord.id, Text))
            == threads.c.thread_id
        )
        .where(AnalysisRunRecord.status.not_in(FINISHED))
        .where(
            or_(
                AnalysisRunRecord.started_at.is_(None),
                AnalysisRunRecord.started_at > func.now() - longest_run,
            )
        )
        .exists()
    )
    return _remove_threads(session, select(threads.c.thread_id).where(~run_in_progress))


def remove_checkpoints_of_research_runs(session: Session, *, longest_run: timedelta) -> int:
    """Remove the checkpoints research runs no longer need; say how many threads.

    The research runs' rule, built like the analysis runs': among the threads
    named for a research run (``research_runs.run_thread_id``, names that start
    with ``RESEARCH_RUN_THREADS``), a thread is kept only while its run is
    unfinished (``queued`` or ``running``) and was started no longer ago than a
    run can take. Threads of any other name, an analysis run's included, are
    not looked at.

    ``longest_run`` is how long after its start a run can still be in
    progress; the caller derives it from how it executes runs. An unfinished
    run started longer ago keeps its status and loses its checkpoints. Like
    the analysis runs' rule, this is maintenance of the whole database, one
    statement, and it does not commit.
    """
    # The library's tables have no model (see ``checkpoint_schema.py``); only this column is used.
    tables = [
        table(name, column("thread_id", Text)) for name in sorted(CHECKPOINT_TABLES_OF_THREADS)
    ]
    threads = union(
        *(
            select(t.c.thread_id).where(
                t.c.thread_id.startswith(RESEARCH_RUN_THREADS, autoescape=True)
            )
            for t in tables
        )
    ).subquery("thread")
    run_in_progress = (
        select(ResearchRunRecord.id)
        # The name is compared as text, the way ``run_thread_id`` builds it. A name whose
        # rest is not a run id therefore never fails; it only finds no run.
        .where(
            func.concat(RESEARCH_RUN_THREADS, cast(ResearchRunRecord.id, Text))
            == threads.c.thread_id
        )
        .where(ResearchRunRecord.status.not_in(RESEARCH_FINISHED))
        .where(
            or_(
                ResearchRunRecord.started_at.is_(None),
                ResearchRunRecord.started_at > func.now() - longest_run,
            )
        )
        .exists()
    )
    return _remove_threads(session, select(threads.c.thread_id).where(~run_in_progress))


def remove_checkpoints_of_studio_runs(session: Session, *, longest_run: timedelta) -> int:
    """Remove the checkpoints studio runs no longer need; say how many threads.

    The studio runs' rule, built like the research runs': among the threads named for a
    studio run (``studio_runs.run_thread_id``, names that start with ``STUDIO_RUN_THREADS``),
    a thread is kept only while its run is unfinished (``queued`` or ``running``) and was
    started no longer ago than a run can take; a run that needs review has finished. Threads
    of any other name are not looked at.

    ``longest_run`` is how long after its start a run can still be in progress, the waits
    for a person's approval of a spend included; the caller derives it from how it executes
    runs. Like the other rules, this is maintenance of the whole database, one statement, and
    it does not commit.
    """
    # The library's tables have no model (see ``checkpoint_schema.py``); only this column is used.
    tables = [
        table(name, column("thread_id", Text)) for name in sorted(CHECKPOINT_TABLES_OF_THREADS)
    ]
    threads = union(
        *(
            select(t.c.thread_id).where(
                t.c.thread_id.startswith(STUDIO_RUN_THREADS, autoescape=True)
            )
            for t in tables
        )
    ).subquery("thread")
    run_in_progress = (
        select(StudioRunRecord.id)
        # Compared as text, as in the research runs' rule.
        .where(
            func.concat(STUDIO_RUN_THREADS, cast(StudioRunRecord.id, Text)) == threads.c.thread_id
        )
        .where(StudioRunRecord.status.not_in(STUDIO_FINISHED))
        .where(
            or_(
                StudioRunRecord.started_at.is_(None),
                StudioRunRecord.started_at > func.now() - longest_run,
            )
        )
        .exists()
    )
    return _remove_threads(session, select(threads.c.thread_id).where(~run_in_progress))


def remove_checkpoints_of_writing_runs(session: Session, *, longest_run: timedelta) -> int:
    """Remove the checkpoints writing runs no longer need; say how many threads.

    The writing runs' rule, built like the research runs': among the threads named for a
    writing run (``writing_runs.run_thread_id``, names that start with
    ``WRITING_RUN_THREADS``), a thread is kept only while its run is unfinished (``queued`` or
    ``running``) and was started no longer ago than a run can take. Threads of any other name
    are not looked at.

    ``longest_run`` is how long after its start a run can still be in progress, the wait for
    a person's decision on its post included; the caller derives it from how it executes runs.
    Like the other rules, this is maintenance of the whole database, one statement, and it
    does not commit.
    """
    # The library's tables have no model (see ``checkpoint_schema.py``); only this column is used.
    tables = [
        table(name, column("thread_id", Text)) for name in sorted(CHECKPOINT_TABLES_OF_THREADS)
    ]
    threads = union(
        *(
            select(t.c.thread_id).where(
                t.c.thread_id.startswith(WRITING_RUN_THREADS, autoescape=True)
            )
            for t in tables
        )
    ).subquery("thread")
    run_in_progress = (
        select(WritingRunRecord.id)
        # Compared as text, as in the research runs' rule.
        .where(
            func.concat(WRITING_RUN_THREADS, cast(WritingRunRecord.id, Text)) == threads.c.thread_id
        )
        .where(WritingRunRecord.status.not_in(WRITING_FINISHED))
        .where(
            or_(
                WritingRunRecord.started_at.is_(None),
                WritingRunRecord.started_at > func.now() - longest_run,
            )
        )
        .exists()
    )
    return _remove_threads(session, select(threads.c.thread_id).where(~run_in_progress))


def remove_stale_checkpoints(session: Session, *, kind: str, untouched_for: timedelta) -> int:
    """Remove the checkpoints of the threads of ``kind`` that nothing wrote for ``untouched_for``;
    say how many threads.

    The rule for a kind of run that has no table of runs to consult, or whose agent did not
    write one: a run in progress stores a checkpoint after every step of its graph, so a
    thread whose newest checkpoint is older than the longest a run of its kind can take
    belongs to no run in progress. ``untouched_for`` is that longest run, and has to count
    everything a run can spend between two steps: an action at all its attempts, and the wait
    for a person's approval. The time of a checkpoint is the one the library writes into it
    (``checkpoint ->> 'ts'``), compared with the database's clock.

    Only threads named ``<kind>:`` that have a checkpoint are looked at; their rows go from
    every table. A thread with rows in the other tables and no checkpoint at all is left: the
    library writes a step's checkpoint with its blobs, and such rows are not known to be stale.
    Like the analysis runs' rule, this is maintenance of the whole database, one statement,
    and it does not commit.
    """
    validate_kind(kind)
    checkpoints = table("checkpoints", column("thread_id", Text), column("checkpoint", JSONB))
    written_at = cast(checkpoints.c.checkpoint["ts"].astext, DateTime(timezone=True))
    stale = (
        select(checkpoints.c.thread_id)
        .where(checkpoints.c.thread_id.startswith(f"{kind}:", autoescape=True))
        .group_by(checkpoints.c.thread_id)
        .having(func.max(written_at) < func.now() - untouched_for)
    )
    return _remove_threads(session, stale)


def _remove_threads(session: Session, threads: Select) -> int:
    """Delete every row of the threads ``threads`` selects, from each table; say how many."""
    # The library's tables have no model (see ``checkpoint_schema.py``); only this column is used.
    tables = [
        table(name, column("thread_id", Text)) for name in sorted(CHECKPOINT_TABLES_OF_THREADS)
    ]
    removable = threads.cte("removable")
    removed = [
        delete(t)
        .where(t.c.thread_id.in_(select(removable.c.thread_id)))
        .returning(t.c.thread_id)
        .cte(f"removed_from_{t.name}")
        for t in tables
    ]
    removed_threads = union(*(select(r.c.thread_id) for r in removed)).subquery("removed")
    return session.execute(select(func.count()).select_from(removed_threads)).scalar_one()
