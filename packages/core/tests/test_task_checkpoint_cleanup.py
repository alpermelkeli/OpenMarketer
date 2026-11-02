"""Tests for removing the checkpoints that orchestrator tasks no longer need.

They need PostgreSQL (see the root ``conftest.py``). Runs and checkpoints are committed, the
way the worker and the checkpoint store write them. The rule is built like the analysis
runs' (``test_checkpoint_cleanup.py``); these tests hold what is the tasks' own: their prefix
and their table, and that the threads of the runs a task started are not theirs.
"""

import uuid
from datetime import timedelta
from typing import TypedDict

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.checkpoint_cleanup import remove_checkpoints_of_tasks
from openmarketer_core.db.checkpoint_schema import CHECKPOINT_TABLES_OF_THREADS
from openmarketer_core.db.models import Project, TaskStatus, Workspace
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.db.tasks import (
    mark_task_failed,
    mark_task_started,
    request_task_run,
    run_thread_id,
)
from openmarketer_core.db.writing_runs import run_thread_id as writing_thread_id
from openmarketer_core.graph_checkpoints.postgres import run_checkpoints
from openmarketer_core.orchestrator.task.plan import TaskRequest

A_DAY = timedelta(days=1)
REQUEST = TaskRequest(request="Post about offline mode", understanding="One post for X.")


class ProjectIds(TypedDict):
    workspace_id: uuid.UUID
    project_id: uuid.UUID


@pytest.fixture
def database_url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


@pytest.fixture
def sessions(database_url) -> sessionmaker[Session]:
    return session_factory(database_url)


def task_that_is(status: TaskStatus, sessions: sessionmaker[Session]) -> uuid.UUID:
    """A task of a new project, taken as far as ``status`` (queued, running or failed)."""
    with transaction(sessions) as session:
        workspace = Workspace(name="Acme")
        session.add(workspace)
        session.flush()
        project = Project(
            workspace_id=workspace.id, name="App", source_repo_url="https://example.com/r.git"
        )
        session.add(project)
        session.flush()
        scope = ProjectIds(workspace_id=workspace.id, project_id=project.id)
        task_id = request_task_run(
            session, **scope, request=REQUEST, requested_by="orchestrator"
        ).id
        if status is TaskStatus.QUEUED:
            return task_id
        mark_task_started(session, **scope, task_id=task_id)
        if status is TaskStatus.FAILED:
            mark_task_failed(session, **scope, task_id=task_id, error="no")
        return task_id


def leave_checkpoints(database_url: str, thread_id: str) -> None:
    """Rows in all three tables of the thread, as a step of the graph leaves them."""
    with run_checkpoints(database_url, thread_id) as run:
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"messages": ["a plan"]}
        checkpoint["channel_versions"] = {"messages": "1"}
        stored = run.store.put(
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
            checkpoint,
            {"source": "loop", "step": 1},
            {"messages": "1"},
        )
        run.store.put_writes(stored, [("notes", "one finished step")], task_id="task-1")


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


def clean_up(sessions: sessionmaker[Session], longest_run: timedelta = A_DAY) -> int:
    with transaction(sessions) as session:
        return remove_checkpoints_of_tasks(session, longest_run=longest_run)


def test_checkpoints_of_a_finished_task_are_removed(sessions, database_url, engine):
    thread = run_thread_id(task_that_is(TaskStatus.FAILED, sessions))
    leave_checkpoints(database_url, thread)

    clean_up(sessions)

    assert tables_holding(engine, thread) == set()


@pytest.mark.parametrize("status", [TaskStatus.QUEUED, TaskStatus.RUNNING])
def test_checkpoints_of_an_unfinished_task_are_kept(status, sessions, database_url, engine):
    thread = run_thread_id(task_that_is(status, sessions))
    leave_checkpoints(database_url, thread)

    clean_up(sessions)

    assert tables_holding(engine, thread) == CHECKPOINT_TABLES_OF_THREADS


def test_a_running_task_started_longer_ago_than_a_task_can_take_loses_them(
    sessions, database_url, engine
):
    task_id = task_that_is(TaskStatus.RUNNING, sessions)
    thread = run_thread_id(task_id)
    leave_checkpoints(database_url, thread)
    with transaction(sessions) as session:
        session.execute(
            text(
                "UPDATE orchestrator_task SET started_at = now() - interval '2 days' WHERE id = :id"
            ),
            {"id": task_id},
        )

    clean_up(sessions)

    assert tables_holding(engine, thread) == set()


def test_checkpoints_of_a_task_that_does_not_exist_are_removed(sessions, database_url, engine):
    thread = run_thread_id(uuid.uuid4())
    leave_checkpoints(database_url, thread)

    assert clean_up(sessions) >= 1
    assert tables_holding(engine, thread) == set()


def test_the_thread_of_a_run_a_task_started_is_not_this_rules(sessions, database_url, engine):
    thread = writing_thread_id(uuid.uuid4())
    leave_checkpoints(database_url, thread)

    clean_up(sessions)

    assert tables_holding(engine, thread) == CHECKPOINT_TABLES_OF_THREADS
