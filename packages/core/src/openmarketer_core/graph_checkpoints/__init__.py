"""Checkpoints of an agent graph: what every graph in this project uses to survive a failed attempt.

A graph that saves a checkpoint after each step keeps them under a thread, one
thread per run of the graph. ``RunCheckpoints`` is that pair: the store and
the run's thread in it. A graph receives one, compiles itself with the store
and is asked to ``forget()`` the thread when the run is over. The store kept
in PostgreSQL is opened in ``graph_checkpoints/postgres.py``; the type is here
on its own so that a graph module can name it without importing a database
driver, and a graph run without checkpoints needs no database at all.

Nothing here is a rule. When an attempt continues from a checkpoint and when
it starts over is the graph's own decision (the analyzer's is
``analyzer.rules.resumption``); which thread belongs to which run is the
caller's (an analysis run's is ``db.analysis_runs.run_thread_id``); which
leftover threads may be removed is ``db/checkpoint_cleanup.py``; and the
tables are created by the migrations (``db/checkpoint_schema.py``).
"""

from __future__ import annotations

from dataclasses import dataclass

from langgraph.checkpoint.base import BaseCheckpointSaver


@dataclass(frozen=True)
class RunCheckpoints:
    """Where one run of a graph keeps its checkpoints: the store, and the run's thread in it."""

    store: BaseCheckpointSaver[str]
    thread_id: str

    def forget(self) -> None:
        """Remove every checkpoint of the run. Nothing happens when it has none."""
        self.store.delete_thread(self.thread_id)


__all__ = ["RunCheckpoints"]
