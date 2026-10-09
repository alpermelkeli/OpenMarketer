"""The graph checkpointer's tables: which they are and which version of them the migrations create.

The tables belong to LangGraph's PostgreSQL checkpointer
(``langgraph-checkpoint-postgres``), not to this package. The library defines
them as a numbered list of statements and reads and writes them with its own
SQL, so they have no model in ``models.py``; Alembic creates them by running
the library's statements and is told here to leave them out of autogenerate.

This module holds no query and builds no checkpointer.
"""

from __future__ import annotations

# The tables that hold a thread's rows, each with the thread in its ``thread_id`` column.
CHECKPOINT_TABLES_OF_THREADS = frozenset({"checkpoints", "checkpoint_blobs", "checkpoint_writes"})
CHECKPOINT_TABLES = CHECKPOINT_TABLES_OF_THREADS | {"checkpoint_migrations"}

# The library numbers its statements from 0 and records each one it has run in
# ``checkpoint_migrations``. This is the last one the Alembic migrations run.
# When the installed library has a later one, add an Alembic migration that
# runs the new statements and raise this number in the same change.
CHECKPOINT_SCHEMA_VERSION = 9
