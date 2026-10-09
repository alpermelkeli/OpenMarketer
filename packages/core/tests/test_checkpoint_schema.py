"""Tests for the graph checkpointer's tables, which the migrations create for the library.

They need PostgreSQL; the fixtures are in the ``conftest.py`` at the repository root.
The checkpointer here is the library's own, opened on the migrated database and
never asked to set itself up.
"""

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.base import MIGRATIONS
from sqlalchemy import Engine, inspect, text

from openmarketer_core.db.checkpoint_schema import CHECKPOINT_SCHEMA_VERSION, CHECKPOINT_TABLES

LIBRARY_SCHEMA = "created_by_the_library"
CHECKPOINT_REVISION = "70a50205df52"
COLUMNS = """
SELECT table_name, ordinal_position, column_name, data_type, is_nullable, column_default
FROM information_schema.columns WHERE table_schema = :schema ORDER BY 1, 2
"""
CONSTRAINTS = """
SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)
FROM pg_constraint WHERE connamespace = CAST(:schema AS regnamespace) ORDER BY 1, 2
"""
INDEXES = "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = :schema ORDER BY 1"


def driver_url(engine: Engine, **query: str) -> str:
    """The engine's database as the URL psycopg itself takes."""
    url = engine.url.set(drivername="postgresql").update_query_dict(query)
    return url.render_as_string(hide_password=False)


@pytest.fixture
def checkpointer(engine) -> Iterator[PostgresSaver]:
    with PostgresSaver.from_conn_string(driver_url(engine)) as saver:
        yield saver


def described(engine: Engine, schema: str) -> dict[str, list[tuple]]:
    """Columns, constraints and indexes of the checkpointer's tables in one schema."""
    with engine.connect() as conn:
        found = {
            "columns": conn.execute(text(COLUMNS), {"schema": schema}).all(),
            "constraints": conn.execute(text(CONSTRAINTS), {"schema": schema}).all(),
            "indexes": conn.execute(text(INDEXES), {"schema": schema}).all(),
        }
    return {
        kind: [
            tuple(str(value).replace(f"{schema}.", "") for value in row)
            for row in rows
            if any(table in str(row[0]) for table in CHECKPOINT_TABLES)
        ]
        for kind, rows in found.items()
    }


def test_migrations_reach_the_schema_version_the_installed_library_expects():
    expected = len(MIGRATIONS) - 1
    assert expected == CHECKPOINT_SCHEMA_VERSION, (
        f"langgraph-checkpoint-postgres expects checkpoint schema version {expected}, "
        f"the Alembic migrations create version {CHECKPOINT_SCHEMA_VERSION}. "
        f"Add a migration that runs the library's MIGRATIONS[{CHECKPOINT_SCHEMA_VERSION + 1}:"
        f"{expected + 1}] and records their numbers in checkpoint_migrations, then set "
        f"CHECKPOINT_SCHEMA_VERSION to {expected} in db/checkpoint_schema.py."
    )


@pytest.fixture
def revision(alembic_config):
    """The module of the revision that creates the checkpoint tables."""
    return ScriptDirectory.from_config(alembic_config).get_revision(CHECKPOINT_REVISION).module


def test_statements_of_the_installed_library_are_the_ones_the_revision_was_written_for(revision):
    statements = revision.statements_to_run(MIGRATIONS)
    assert len(statements) == CHECKPOINT_SCHEMA_VERSION + 1
    assert revision.statements_digest(statements) == revision.STATEMENTS_SHA256


def test_revision_hashes_the_statements_as_it_sends_them(revision):
    statements = revision.statements_to_run(MIGRATIONS)
    assert not any("CONCURRENTLY" in statement for statement in statements)
    assert any("CONCURRENTLY" in statement for statement in MIGRATIONS)


@pytest.mark.parametrize("number", [0, 5, CHECKPOINT_SCHEMA_VERSION])
def test_revision_refuses_to_run_a_statement_the_library_words_differently(revision, number):
    reworded = list(MIGRATIONS)
    reworded[number] += "; ALTER TABLE checkpoints ADD COLUMN extra TEXT"
    with pytest.raises(RuntimeError, match="words its schema migrations 0 to 9 differently"):
        revision.statements_to_run(reworded)


def test_statement_the_library_adds_later_does_not_change_what_the_revision_runs(revision):
    longer = [*MIGRATIONS, "ALTER TABLE checkpoints ADD COLUMN later TEXT"]
    assert revision.statements_to_run(longer) == revision.statements_to_run(MIGRATIONS)


def test_statements_split_differently_do_not_have_the_same_hash(revision):
    assert revision.statements_digest(["ab", "c"]) != revision.statements_digest(["a", "bc"])


def test_revision_sends_nothing_else_than_the_hashed_statements(revision, monkeypatch):
    sent: list[str] = []

    class Connection:
        def exec_driver_sql(self, statement: str, parameters: object = None) -> None:
            sent.append(statement)

    monkeypatch.setattr(revision.op, "get_bind", Connection)
    revision.upgrade()
    assert sent[0::2] == revision.statements_to_run(MIGRATIONS)
    assert set(sent[1::2]) == {"INSERT INTO checkpoint_migrations (v) VALUES (%(number)s)"}


def test_migrations_create_the_checkpoint_tables(engine):
    assert CHECKPOINT_TABLES <= set(inspect(engine).get_table_names())


def test_database_records_every_library_migration_as_run(engine):
    with engine.connect() as conn:
        recorded = conn.execute(text("SELECT v FROM checkpoint_migrations ORDER BY v")).scalars()
        assert list(recorded) == list(range(CHECKPOINT_SCHEMA_VERSION + 1))


def test_tables_are_the_ones_the_library_creates_for_itself(engine):
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{LIBRARY_SCHEMA}"'))
    try:
        url = driver_url(engine, options=f"-csearch_path={LIBRARY_SCHEMA}")
        with PostgresSaver.from_conn_string(url) as saver:
            saver.setup()
        by_the_library = described(engine, LIBRARY_SCHEMA)
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{LIBRARY_SCHEMA}" CASCADE'))

    assert by_the_library["columns"]
    assert described(engine, "public") == by_the_library


def test_checkpoint_is_written_and_read_back_without_setup(checkpointer):
    thread = {"configurable": {"thread_id": "run-1", "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"facts": ["reads package.json"]}
    checkpoint["channel_versions"] = {"facts": "1"}

    stored = checkpointer.put(thread, checkpoint, {"source": "loop", "step": 1}, {"facts": "1"})
    checkpointer.put_writes(stored, [("notes", "one finished step")], task_id="task-1")

    found = checkpointer.get_tuple(thread)
    assert found is not None
    assert found.checkpoint["id"] == checkpoint["id"]
    assert found.checkpoint["channel_values"] == {"facts": ["reads package.json"]}
    assert found.metadata["step"] == 1
    assert found.pending_writes == [("task-1", "notes", "one finished step")]


def test_deleting_a_thread_removes_its_rows_and_no_other_thread(engine, checkpointer):
    for thread_id in ("run-finished", "run-unfinished"):
        thread = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"facts": ["reads package.json"]}
        checkpoint["channel_versions"] = {"facts": "1"}
        stored = checkpointer.put(thread, checkpoint, {"step": 1}, {"facts": "1"})
        checkpointer.put_writes(stored, [("notes", "one finished step")], task_id="task-1")

    checkpointer.delete_thread("run-finished")

    with engine.connect() as conn:
        threads_left = {
            table: set(conn.execute(text(f"SELECT thread_id FROM {table}")).scalars())
            for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
        }
    assert all("run-finished" not in threads for threads in threads_left.values())
    assert all("run-unfinished" in threads for threads in threads_left.values())


def test_downgrade_removes_the_checkpoint_tables(alembic_config, engine):
    # Runs last: it leaves the temporary database without the tables.
    engine.dispose()
    command.downgrade(alembic_config, "5a790b0f56b1")
    assert not CHECKPOINT_TABLES & set(inspect(engine).get_table_names())
