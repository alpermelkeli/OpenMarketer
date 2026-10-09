"""Tests for the checkpoint store of the agent graphs (``graph_checkpoints``).

The store is opened on a migrated PostgreSQL database (fixtures in the
``conftest.py`` at the repository root), except where the URL is only
converted or points at nothing.
"""

import platform

import ormsgpack
import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import DictRow
from sqlalchemy import Engine, text

from openmarketer_core.db.session import DatabaseError
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.graph_checkpoints.postgres import (
    AttemptInProgress,
    driver_connection_string,
    forget_checkpoints,
    run_checkpoints,
)

# Port 1 is reserved and nothing listens on it.
UNREACHABLE_URL = "postgresql+psycopg://nobody:secret@127.0.0.1:1/nothing?connect_timeout=2"


@pytest.fixture
def database_url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def store_one_checkpoint(run: RunCheckpoints) -> None:
    run.store.put(
        {"configurable": {"thread_id": run.thread_id, "checkpoint_ns": ""}},
        empty_checkpoint(),
        {"source": "loop", "step": 1},
        {},
    )


def connection_of(run: RunCheckpoints) -> Connection[DictRow]:
    assert isinstance(run.store, PostgresSaver)
    assert isinstance(run.store.conn, Connection)
    return run.store.conn


def threads_with_checkpoints(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(conn.execute(text("SELECT thread_id FROM checkpoints")).scalars())


# ------------------------------------------------------------ the database URL
def test_driver_suffix_of_the_url_is_not_part_of_the_connection_string():
    converted = driver_connection_string("postgresql+psycopg://app:pw@db.internal:5433/marketer")
    assert conninfo_to_dict(converted) == {
        "host": "db.internal",
        "port": "5433",
        "user": "app",
        "password": "pw",
        "dbname": "marketer",
    }


@pytest.mark.parametrize("scheme", ["postgresql", "postgresql+psycopg", "postgresql+asyncpg"])
def test_any_postgresql_driver_in_the_url_names_the_same_database(scheme):
    converted = driver_connection_string(f"{scheme}://app:pw@db.internal/marketer")
    assert conninfo_to_dict(converted) == {
        "host": "db.internal",
        "user": "app",
        "password": "pw",
        "dbname": "marketer",
    }


def test_password_with_special_characters_survives_the_conversion():
    url = "postgresql+psycopg://app:p%40ss%20w%2F%3A%27x%5C%3D@db.internal/marketer"
    assert conninfo_to_dict(driver_connection_string(url))["password"] == "p@ss w/:'x\\="


def test_query_options_of_the_url_are_connection_parameters():
    url = (
        "postgresql+psycopg://app@db.internal/marketer"
        "?sslmode=require&connect_timeout=3&options=-csearch_path%3Dtenant"
    )
    parameters = conninfo_to_dict(driver_connection_string(url))
    assert (parameters["sslmode"], parameters["connect_timeout"]) == ("require", "3")
    assert parameters["options"] == "-csearch_path=tenant"


def test_socket_folder_in_the_query_is_the_host():
    converted = driver_connection_string("postgresql+psycopg://app@/marketer?host=/var/run/pg")
    assert conninfo_to_dict(converted) == {
        "host": "/var/run/pg",
        "user": "app",
        "dbname": "marketer",
    }


@pytest.mark.parametrize(
    "url",
    [
        "not a url",
        "postgresql+psycopg://app:hunter2@host:notaport/db",
        "sqlite:///hunter2.db",
        "mysql://app:hunter2@host/db",
    ],
)
def test_url_that_is_not_of_a_postgresql_database_is_a_database_error(url):
    with pytest.raises(DatabaseError, match="invalid database URL") as raised:
        driver_connection_string(url)
    assert "hunter2" not in str(raised.value)
    assert raised.value.__cause__ is None


# ------------------------------------------------------------------ the store
def test_unreachable_database_is_a_database_error():
    with pytest.raises(DatabaseError) as raised:
        with run_checkpoints(UNREACHABLE_URL, "run-1"):
            pass
    assert "secret" not in str(raised.value)


def test_store_works_on_the_migrated_tables(database_url, engine):
    with run_checkpoints(database_url, "run-stored") as run:
        store_one_checkpoint(run)
    assert "run-stored" in threads_with_checkpoints(engine)


def test_connection_is_closed_when_the_block_ends(database_url):
    with run_checkpoints(database_url, "run-closed") as run:
        connection = connection_of(run)
        assert not connection.closed
    assert connection.closed


def test_each_run_has_a_connection_of_its_own(database_url):
    with run_checkpoints(database_url, "run-a") as a, run_checkpoints(database_url, "run-b") as b:
        assert connection_of(a) is not connection_of(b)


def test_forgetting_removes_only_the_checkpoints_of_the_run(database_url, engine):
    with run_checkpoints(database_url, "run-over") as over:
        store_one_checkpoint(over)
    with run_checkpoints(database_url, "run-going") as going:
        store_one_checkpoint(going)

    with run_checkpoints(database_url, "run-over") as over:
        over.forget()
        over.forget()

    assert {"run-over", "run-going"} & threads_with_checkpoints(engine) == {"run-going"}


def test_database_that_stops_answering_is_a_database_error(database_url):
    with run_checkpoints(database_url, "run-cut-off") as run:
        connection_of(run).close()
        with pytest.raises(DatabaseError, match="connection is closed"):
            store_one_checkpoint(run)
        with pytest.raises(DatabaseError, match="connection is closed"):
            run.forget()


def test_failure_of_the_callers_own_work_is_not_reworded(database_url):
    with pytest.raises(RuntimeError, match="analysis went wrong"):
        with run_checkpoints(database_url, "run-1"):
            raise RuntimeError("analysis went wrong")


def test_url_option_the_driver_refuses_is_a_database_error_without_the_url():
    url = "postgresql+psycopg://app:hunter2@db.internal/marketer?no_such_option=secret-value"
    with pytest.raises(DatabaseError, match="invalid database URL") as raised:
        driver_connection_string(url)
    assert "hunter2" not in str(raised.value)
    assert "secret-value" not in str(raised.value)
    assert raised.value.__cause__ is None


# ------------------------------------------------- stored bytes are data, not code
# The library's encoding of "import ``platform`` and call ``node()``": extension code 1
# is a constructor with positional arguments, as (module, name, arguments).
CALLS_PLATFORM_NODE = ormsgpack.packb(ormsgpack.Ext(1, ormsgpack.packb(("platform", "node", []))))


def store_a_step(run: RunCheckpoints, messages: list) -> None:
    """A checkpoint with a channel in ``checkpoint_blobs`` and a write in ``checkpoint_writes``."""
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": messages, "step": 3, "model": "fake/model"}
    checkpoint["channel_versions"] = {"messages": "1", "step": "1", "model": "1"}
    stored = run.store.put(
        {"configurable": {"thread_id": run.thread_id, "checkpoint_ns": ""}},
        checkpoint,
        {"source": "loop", "step": 1},
        {"messages": "1", "step": "1", "model": "1"},
    )
    run.store.put_writes(stored, [("notes", ["one finished step"])], task_id="task-1")


def read_back(run: RunCheckpoints):
    found = run.store.get_tuple({"configurable": {"thread_id": run.thread_id}})
    assert found is not None
    return found


@pytest.mark.parametrize("table", ["checkpoint_blobs", "checkpoint_writes"])
def test_stored_bytes_that_name_a_callable_do_not_call_it_when_read(database_url, engine, table):
    thread = f"run-tampered-{table}"
    with run_checkpoints(database_url, thread) as run:
        store_a_step(run, [{"role": "user", "content": "Repository map"}])
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"UPDATE {table} SET type = 'msgpack', blob = :blob WHERE thread_id = :thread"
                ),
                {"blob": CALLS_PLATFORM_NODE, "thread": thread},
            )
        found = read_back(run)

    assert found.pending_writes
    read = {
        "checkpoint_blobs": found.checkpoint["channel_values"]["messages"],
        "checkpoint_writes": found.pending_writes[0][2],
    }[table]
    assert read != platform.node()
    assert read == []


def test_state_of_plain_data_is_read_back_as_it_was_stored(database_url):
    messages = [
        {"role": "system", "content": "You analyse a repository."},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "1: # Memoria\n"},
    ]
    with run_checkpoints(database_url, "run-plain-data") as run:
        store_a_step(run, messages)
        found = read_back(run)
    assert found.checkpoint["channel_values"] == {
        "messages": messages,
        "step": 3,
        "model": "fake/model",
    }
    assert found.pending_writes == [("task-1", "notes", ["one finished step"])]


# ------------------------------------------------------- one attempt at a time
def test_second_attempt_at_a_run_is_refused_while_the_first_holds_its_checkpoints(database_url):
    with run_checkpoints(database_url, "run-held"):
        with pytest.raises(AttemptInProgress, match="run-held"):
            with run_checkpoints(database_url, "run-held"):
                pytest.fail("two attempts hold the same thread")


def test_refused_attempt_writes_nothing_to_the_thread(database_url, engine):
    with run_checkpoints(database_url, "run-not-interleaved"):
        with pytest.raises(AttemptInProgress):
            with run_checkpoints(database_url, "run-not-interleaved") as second:
                store_one_checkpoint(second)
    assert "run-not-interleaved" not in threads_with_checkpoints(engine)


def test_next_attempt_holds_the_checkpoints_once_the_first_has_ended(database_url, engine):
    with run_checkpoints(database_url, "run-retried") as first:
        store_one_checkpoint(first)
    with run_checkpoints(database_url, "run-retried") as second:
        assert second.store.get_tuple({"configurable": {"thread_id": "run-retried"}}) is not None


def test_attempt_whose_connection_was_lost_no_longer_holds_the_checkpoints(database_url):
    with run_checkpoints(database_url, "run-of-a-dead-worker") as first:
        connection_of(first).close()
        with run_checkpoints(database_url, "run-of-a-dead-worker"):
            pass


def test_attempts_at_different_runs_do_not_refuse_each_other(database_url):
    with run_checkpoints(database_url, "run-one"), run_checkpoints(database_url, "run-two"):
        pass


# ------------------------------------------------- forgetting without an attempt
def test_checkpoints_of_a_run_are_forgotten_without_holding_them(database_url, engine):
    with run_checkpoints(database_url, "run-ended") as run:
        store_one_checkpoint(run)
    forget_checkpoints(database_url, "run-ended")
    forget_checkpoints(database_url, "run-ended")
    assert "run-ended" not in threads_with_checkpoints(engine)


def test_checkpoints_are_forgotten_while_an_attempt_holds_them(database_url, engine):
    with run_checkpoints(database_url, "run-given-up-on") as still_running:
        store_one_checkpoint(still_running)
        forget_checkpoints(database_url, "run-given-up-on")
    assert "run-given-up-on" not in threads_with_checkpoints(engine)


def test_forgetting_in_an_unreachable_database_is_a_database_error():
    with pytest.raises(DatabaseError):
        forget_checkpoints(UNREACHABLE_URL, "run-1")
