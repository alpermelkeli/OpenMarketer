"""Tests for removing the checkpoints of a kind of run that nothing wrote for too long.

They need PostgreSQL (see the root ``conftest.py``). Checkpoints are stored by the checkpoint
store and committed, and a test moves the time written in them into the past.
"""

import uuid
from datetime import timedelta

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.checkpoint_cleanup import remove_stale_checkpoints
from openmarketer_core.db.checkpoint_schema import CHECKPOINT_TABLES_OF_THREADS
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.graph_checkpoints.postgres import run_checkpoints

A_DAY = timedelta(days=1)
KIND = "writer-run"


@pytest.fixture
def database_url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


@pytest.fixture
def sessions(database_url) -> sessionmaker[Session]:
    return session_factory(database_url)


def a_thread(kind: str = KIND) -> str:
    return f"{kind}:{uuid.uuid4()}"


def leave_a_step(database_url: str, thread_id: str) -> None:
    """Rows in all three tables of the thread, as a step of a graph leaves them."""
    with run_checkpoints(database_url, thread_id) as run:
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"messages": ["what the run was working on"]}
        checkpoint["channel_versions"] = {"messages": "1"}
        stored = run.store.put(
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
            checkpoint,
            {"source": "loop", "step": 1},
            {"messages": "1"},
        )
        run.store.put_writes(stored, [("notes", "one finished step")], task_id="task-1")


def written(engine: Engine, thread_id: str, ago: timedelta) -> None:
    """Move the time written in every checkpoint of the thread into the past."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE checkpoints SET checkpoint = jsonb_set(checkpoint, '{ts}',"
                " to_jsonb(to_char((now() - :ago) AT TIME ZONE 'UTC',"
                ' \'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"\')))'
                " WHERE thread_id = :thread"
            ),
            {"ago": ago, "thread": thread_id},
        )


def tables_holding(engine: Engine, thread_id: str) -> set[str]:
    with engine.connect() as conn:
        return {
            table
            for table in CHECKPOINT_TABLES_OF_THREADS
            if conn.execute(
                text(f"SELECT count(*) FROM {table} WHERE thread_id = :thread"),
                {"thread": thread_id},
            ).scalar_one()
        }


def clean_up(sessions: sessionmaker[Session], kind: str = KIND) -> int:
    with transaction(sessions) as session:
        return remove_stale_checkpoints(session, kind=kind, untouched_for=A_DAY)


def test_thread_nothing_wrote_for_longer_than_a_run_takes_is_removed_from_every_table(
    sessions, database_url, engine
):
    thread = a_thread()
    leave_a_step(database_url, thread)
    written(engine, thread, A_DAY + timedelta(minutes=1))

    clean_up(sessions)

    assert tables_holding(engine, thread) == set()


def test_thread_written_within_the_time_a_run_takes_is_kept(sessions, database_url, engine):
    thread = a_thread()
    leave_a_step(database_url, thread)
    written(engine, thread, A_DAY - timedelta(minutes=1))

    clean_up(sessions)

    assert tables_holding(engine, thread) == CHECKPOINT_TABLES_OF_THREADS


def test_thread_is_judged_by_its_newest_checkpoint(sessions, database_url, engine):
    thread = a_thread()
    leave_a_step(database_url, thread)
    written(engine, thread, 2 * A_DAY)
    leave_a_step(database_url, thread)  # a later step, written now

    clean_up(sessions)

    assert tables_holding(engine, thread) == CHECKPOINT_TABLES_OF_THREADS


@pytest.mark.parametrize("kind", ["analysis-run", "writer-run-draft"])
def test_thread_of_another_kind_is_kept_whatever_its_age(sessions, database_url, engine, kind):
    # "writer-run-draft" starts with the kind's name: the colon after a kind tells them apart.
    thread = a_thread(kind)
    leave_a_step(database_url, thread)
    written(engine, thread, 30 * A_DAY)

    clean_up(sessions)

    assert tables_holding(engine, thread) == CHECKPOINT_TABLES_OF_THREADS


def test_cleanup_says_how_many_threads_it_removed(sessions, database_url, engine):
    for _ in range(2):
        thread = a_thread()
        leave_a_step(database_url, thread)
        written(engine, thread, 2 * A_DAY)
    leave_a_step(database_url, a_thread())

    assert clean_up(sessions) == 2
    assert clean_up(sessions) == 0


def test_kind_that_cannot_name_a_thread_is_refused(sessions):
    with pytest.raises(ValueError):
        clean_up(sessions, kind="Writer Run")
