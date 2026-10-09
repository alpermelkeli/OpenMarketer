"""The checkpoint store of the agent graphs, kept in PostgreSQL by LangGraph's own checkpointer.

Every graph that checkpoints uses this store; nothing in it is about one of
them. ``run_checkpoints`` opens it for one attempt at a run on a connection of
its own, so several runs can be executed at once, each from its own thread.
What it yields goes to the graph (the analyzer's ``analyze``, through
``analyze_repository``) and is asked to ``forget()`` the run when the run is
over. One attempt at a time holds a run's checkpoints: a second one is refused
(``AttemptInProgress``) instead of writing to the same thread.
``forget_checkpoints`` removes a run's checkpoints without holding them, for a
caller that only records that the run is over. A failure of the store is a
``DatabaseError``, like any other failure of the database.

What is read back from the store is treated as data, never as something to
run. The library's serializer can rebuild objects by importing a module and
calling a name that the stored bytes give, and by default does so for any
name; whoever can write the checkpoint tables would then run code in the
process that holds the model provider's key and the repository tokens. The
store handed out here is therefore built with the strict serializer, set in
code and not by the environment: only the library's short list of plain value
types is rebuilt, anything else comes back as the plain data it was stored
with. A graph's state must therefore be plain data, as the analyzer's is.

This module does not create the tables: the migrations do (see
``db/checkpoint_schema.py``), and the library's ``setup()`` is never called.
It holds no rule about when an attempt continues from a checkpoint (each graph
has its own; the analyzer's is
``repository_analysis.analyzer_agent.rules.resumption``), does not name the
thread of a run (the caller does; an analysis run's is
``db.analysis_runs.run_thread_id``) and has no cleanup rule: its caller
forgets a run that is over, and the rule of its kind of run removes what was
left behind (the analysis runs' is in ``db/checkpoint_cleanup.py``). It does
not read the environment: the caller passes the database URL.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import Connection, Pipeline
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow
from sqlalchemy import make_url
from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg
from sqlalchemy.exc import SQLAlchemyError

from openmarketer_core.db.session import DatabaseError
from openmarketer_core.graph_checkpoints import RunCheckpoints

# Advisory locks are numbers shared by the whole database. A thread's is the hash of its
# id under this seed, so it is not the number other code would derive from the same text.
ATTEMPT_LOCK_SEED = 7_233_001


class AttemptInProgress(Exception):
    """Another attempt at the run holds its checkpoints; two must not write one thread."""

    def __init__(self, thread_id: str) -> None:
        super().__init__(f"another attempt holds the checkpoints of thread {thread_id}")


@contextmanager
def run_checkpoints(database_url: str, thread_id: str) -> Iterator[RunCheckpoints]:
    """The checkpoints of the run ``thread_id``, on one connection that is closed at the end.

    The block is one attempt at the run and holds the thread for as long as it
    lasts: opening it while another attempt (in this process or any other) has
    it raises ``AttemptInProgress`` at once, without waiting. The hold is a
    lock of the connection's session, so the database releases it when the
    block ends and also when the process dies or the connection is lost.

    ``database_url`` is the project's SQLAlchemy URL. Raises ``DatabaseError``
    when the URL is not one of a PostgreSQL database or the database cannot be
    reached; what is yielded raises it when the database stops answering.
    """
    with _store(database_url) as store:
        if not store.hold_thread(thread_id):
            raise AttemptInProgress(thread_id)
        yield RunCheckpoints(store=store, thread_id=thread_id)


def forget_checkpoints(database_url: str, thread_id: str) -> None:
    """Remove every checkpoint of the run ``thread_id``, held by an attempt or not.

    For a run that is over: an attempt still running then has nothing to
    continue. Nothing happens when the run has no checkpoints. Raises
    ``DatabaseError`` like ``run_checkpoints``.
    """
    with _store(database_url) as store:
        store.delete_thread(thread_id)


@contextmanager
def _store(database_url: str) -> Iterator[_PostgresCheckpoints]:
    connection_string = driver_connection_string(database_url)
    with _driver_errors_as_database_errors():
        with _PostgresCheckpoints.from_conn_string(connection_string) as store:
            # The class method is typed as giving the library's own class.
            assert isinstance(store, _PostgresCheckpoints)
            yield store


def driver_connection_string(database_url: str) -> str:
    """``database_url`` as the connection string the PostgreSQL driver takes.

    The result holds the password: it is for the driver only, never for a log
    or an error.
    """
    # Neither error below repeats the URL: it holds the password.
    try:
        url = make_url(database_url)
    except (SQLAlchemyError, ValueError):
        raise DatabaseError("invalid database URL: it could not be parsed") from None
    if url.get_backend_name() != "postgresql":
        raise DatabaseError("invalid database URL: checkpoints need a PostgreSQL database")
    # SQLAlchemy's own translation, so both connect to the same database with the same options.
    _, parameters = PGDialect_psycopg().create_connect_args(url)
    try:
        return make_conninfo(**parameters)
    except psycopg.Error:
        # The driver names the option it refuses; the URL's query is not ours to repeat.
        raise DatabaseError(
            "invalid database URL: it has an option the PostgreSQL driver does not accept"
        ) from None


@contextmanager
def _driver_errors_as_database_errors() -> Iterator[None]:
    try:
        yield
    except psycopg.Error as e:
        raise DatabaseError(_first_line(e)) from e


def _first_line(error: psycopg.Error) -> str:
    """The driver's first line names the problem; the rest is detail for a log."""
    lines = str(error).strip().splitlines()
    return lines[0] if lines else type(error).__name__


class _PostgresCheckpoints(PostgresSaver):
    """The library's checkpointer, reading stored bytes strictly and raising ``DatabaseError``.

    See the module docstring for why the serializer is strict.
    """

    def __init__(self, conn: Connection[DictRow], pipe: Pipeline | None = None) -> None:
        # ``None`` allows the library's list of plain value types and nothing else.
        super().__init__(conn, pipe, serde=JsonPlusSerializer(allowed_msgpack_modules=None))

    def hold_thread(self, thread_id: str) -> bool:
        """Take the thread for this connection's session; ``False`` when another has it."""
        with _driver_errors_as_database_errors(), self._cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, %s)) AS held",
                (thread_id, ATTEMPT_LOCK_SEED),
            )
            row = cursor.fetchone()
        return bool(row and row["held"])

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        with _driver_errors_as_database_errors():
            return super().get_tuple(config)

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        with _driver_errors_as_database_errors():
            return super().put(config, checkpoint, metadata, new_versions)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        with _driver_errors_as_database_errors():
            super().put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        with _driver_errors_as_database_errors():
            super().delete_thread(thread_id)
