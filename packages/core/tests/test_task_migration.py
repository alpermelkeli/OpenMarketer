"""Tests for the migration that adds orchestrator tasks and their steps.

They need PostgreSQL (see the root ``conftest.py``) and run the migrations themselves, so they
do not use the ``engine`` fixture. The rows are written at the revision itself, in SQL of the
revision's own columns, and the downgrade is then run over them.
"""

import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

BEFORE = "2530b8dce085"
ADDS_TASKS = "0a4fdc44993b"


@pytest.fixture(scope="module")
def database(alembic_config):
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    yield engine
    engine.dispose()


def tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def columns(engine, table: str) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns(table)}


def count(engine, query: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(query)).scalar_one()


def a_task_the_chat_heard_about(engine) -> None:
    """A finished task with a step, its confirmation, its event, and the chat's follow-up."""
    with engine.begin() as conn:

        def run(sql: str, **values: object) -> object:
            result = conn.execute(text(sql), values)
            return result.scalar() if result.returns_rows else None

        workspace = run("INSERT INTO workspace (name) VALUES ('Acme') RETURNING id")
        project = run(
            "INSERT INTO project (workspace_id, name, source_repo_url)"
            " VALUES (:w, 'App', 'https://example.com/a.git') RETURNING id",
            w=workspace,
        )
        chat = run(
            "INSERT INTO chat_thread (project_id, title, last_activity_at)"
            " VALUES (:p, 'Launch', now()) RETURNING id",
            p=project,
        )
        task = run(
            "INSERT INTO orchestrator_task (project_id, status, outcome, request, understanding,"
            " chat_thread_id, plan, report, started_at, finished_at) VALUES (:p, 'succeeded',"
            " 'completed', 'Post', 'One post.', :c, '{}', '{}', now(), now()) RETURNING id",
            p=project,
            c=chat,
        )
        run(
            "INSERT INTO orchestrator_task_step (project_id, task_id, step_key, agent,"
            " child_run_id) VALUES (:p, :t, 'post', 'writing', :r)",
            p=project,
            t=task,
            r=uuid.uuid4(),
        )
        run(
            "INSERT INTO proposal (project_id, run_kind, run_id, workflow_id, action_id,"
            " action_name, effect, payload, expires_at) VALUES (:p, 'orchestrator-task', :t,"
            " 'orchestrator-task-1', 'confirm-1', 'confirm_plan', 'confirms', '{}',"
            " now() + interval '1 day')",
            p=project,
            t=task,
        )
        run(
            "INSERT INTO run_event (project_id, run_kind, run_id, kind, detail)"
            " VALUES (:p, 'orchestrator-task', :t, 'task_planned', '{}')",
            p=project,
            t=task,
        )
        turn = run(
            "INSERT INTO chat_turn (project_id, thread_id, trigger, task_id, status, finished_at)"
            " VALUES (:p, :c, 'task_finished', :t, 'answered', now()) RETURNING id",
            p=project,
            c=chat,
            t=task,
        )
        run(
            "INSERT INTO chat_message (project_id, thread_id, turn_id, role, content)"
            " VALUES (:p, :c, :turn, 'assistant', 'The post is proposed.')",
            p=project,
            c=chat,
            turn=turn,
        )
        run(
            "INSERT INTO run_event (project_id, run_kind, run_id, kind, detail)"
            " VALUES (:p, 'orchestrator-chat', :turn, 'turn_started', '{}')",
            p=project,
            turn=turn,
        )


def test_upgrade_adds_the_tables_and_columns(alembic_config, database):
    command.upgrade(alembic_config, ADDS_TASKS)

    assert {"orchestrator_task", "orchestrator_task_step"} <= tables(database)
    assert "task_id" in columns(database, "chat_turn")
    assert "origin_action_id" in columns(database, "writing_run")


def test_a_follow_up_on_a_task_names_the_task(alembic_config, database):
    command.upgrade(alembic_config, ADDS_TASKS)

    with pytest.raises(IntegrityError, match="ck_chat_turn_task_id"), database.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO chat_turn (project_id, thread_id, trigger)"
                " VALUES (gen_random_uuid(), gen_random_uuid(), 'task_finished')"
            )
        )


def test_downgrade_drops_the_tasks_with_what_names_them(alembic_config, database):
    command.upgrade(alembic_config, ADDS_TASKS)
    a_task_the_chat_heard_about(database)

    command.downgrade(alembic_config, BEFORE)

    assert not {"orchestrator_task", "orchestrator_task_step"} & tables(database)
    assert "task_id" not in columns(database, "chat_turn")
    assert "origin_action_id" not in columns(database, "writing_run")
    assert count(database, "SELECT count(*) FROM proposal") == 0
    assert count(database, "SELECT count(*) FROM run_event") == 0
    assert count(database, "SELECT count(*) FROM chat_turn") == 0
    assert count(database, "SELECT count(*) FROM chat_message WHERE turn_id IS NULL") == 1


def test_upgrade_can_be_repeated_after_a_downgrade(alembic_config, database):
    command.upgrade(alembic_config, ADDS_TASKS)

    assert "orchestrator_task" in tables(database)
