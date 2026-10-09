"""Tests for removing the checkpoints that no analysis run needs any more.

They need PostgreSQL (see the root ``conftest.py``). Runs and checkpoints are
committed, the way the worker and the checkpoint store write them.
"""

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.analysis_runs import (
    mark_run_failed,
    mark_run_started,
    mark_run_succeeded,
    request_analysis_run,
    run_thread_id,
)
from openmarketer_core.db.checkpoint_cleanup import remove_unneeded_checkpoints
from openmarketer_core.db.checkpoint_schema import CHECKPOINT_TABLES_OF_THREADS
from openmarketer_core.db.evidence_store import save_analysis_of_project
from openmarketer_core.db.models import AnalysisRunStatus, Project, Workspace
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.graph_checkpoints.postgres import run_checkpoints
from openmarketer_core.intake import Snapshot
from openmarketer_core.profile import ProductProfile

A_DAY = timedelta(days=1)
PROFILE = ProductProfile.model_validate({"product": {"name": "Example App", "type": "dev_tool"}})


@pytest.fixture
def database_url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


@pytest.fixture
def sessions(database_url) -> sessionmaker[Session]:
    return session_factory(database_url)


def run_that_is(status: AnalysisRunStatus, sessions: sessionmaker[Session]) -> uuid.UUID:
    """A run of a new project, taken as far as ``status``."""
    with transaction(sessions) as session:
        workspace = Workspace(name="Acme")
        session.add(workspace)
        session.flush()
        project = Project(
            workspace_id=workspace.id, name="App", source_repo_url="https://example.com/r.git"
        )
        session.add(project)
        session.flush()
        scope = {"workspace_id": workspace.id, "project_id": project.id}
        run_id = request_analysis_run(session, **scope).id
        if status is AnalysisRunStatus.QUEUED:
            return run_id
        mark_run_started(session, **scope, run_id=run_id)
        if status is AnalysisRunStatus.FAILED:
            mark_run_failed(session, **scope, run_id=run_id, error="no")
        if status is AnalysisRunStatus.SUCCEEDED:
            snapshot = Snapshot(
                root=Path("."),
                source_url="https://example.com/r.git",
                commit_sha="a" * 40,
                ref="main",
            )
            saved = save_analysis_of_project(
                session, **scope, snapshot=snapshot, facts=[], profile=PROFILE
            )
            mark_run_succeeded(session, **scope, run_id=run_id, profile_id=saved.profile_id)
        return run_id


def leave_checkpoints(database_url: str, thread_id: str) -> None:
    """Rows in all three tables of the thread, as a step of the graph leaves them."""
    with run_checkpoints(database_url, thread_id) as run:
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"messages": ["a line of the repository"]}
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
        return remove_unneeded_checkpoints(session, longest_run=longest_run)


def started(run_id: uuid.UUID, ago: timedelta, sessions: sessionmaker[Session]) -> None:
    """Move the start of a running run into the past."""
    with transaction(sessions) as session:
        session.execute(
            text("UPDATE analysis_run SET started_at = now() - :ago WHERE id = :id"),
            {"ago": ago, "id": run_id},
        )


@pytest.mark.parametrize("status", [AnalysisRunStatus.SUCCEEDED, AnalysisRunStatus.FAILED])
def test_checkpoints_of_a_finished_run_are_removed_from_every_table(
    status, sessions, database_url, engine
):
    thread = run_thread_id(run_that_is(status, sessions))
    leave_checkpoints(database_url, thread)
    assert tables_holding(engine, thread) == CHECKPOINT_TABLES_OF_THREADS

    clean_up(sessions)

    assert tables_holding(engine, thread) == set()


@pytest.mark.parametrize("status", [AnalysisRunStatus.QUEUED, AnalysisRunStatus.RUNNING])
def test_checkpoints_of_an_unfinished_run_are_kept(status, sessions, database_url, engine):
    thread = run_thread_id(run_that_is(status, sessions))
    leave_checkpoints(database_url, thread)

    clean_up(sessions)

    assert tables_holding(engine, thread) == CHECKPOINT_TABLES_OF_THREADS


def test_unfinished_run_started_longer_ago_than_a_run_can_take_loses_its_checkpoints(
    sessions, database_url, engine
):
    run_id = run_that_is(AnalysisRunStatus.RUNNING, sessions)
    started(run_id, timedelta(hours=3), sessions)
    leave_checkpoints(database_url, run_thread_id(run_id))

    clean_up(sessions, longest_run=timedelta(hours=2))

    assert tables_holding(engine, run_thread_id(run_id)) == set()


def test_unfinished_run_started_within_the_time_a_run_can_take_keeps_its_checkpoints(
    sessions, database_url, engine
):
    run_id = run_that_is(AnalysisRunStatus.RUNNING, sessions)
    started(run_id, timedelta(hours=1), sessions)
    leave_checkpoints(database_url, run_thread_id(run_id))

    clean_up(sessions, longest_run=timedelta(hours=2))

    assert tables_holding(engine, run_thread_id(run_id)) == CHECKPOINT_TABLES_OF_THREADS


def test_run_that_lost_its_checkpoints_to_its_age_is_still_running(sessions, database_url):
    run_id = run_that_is(AnalysisRunStatus.RUNNING, sessions)
    started(run_id, timedelta(hours=3), sessions)
    leave_checkpoints(database_url, run_thread_id(run_id))

    clean_up(sessions, longest_run=timedelta(hours=2))

    with transaction(sessions) as session:
        status = session.execute(
            text("SELECT status FROM analysis_run WHERE id = :id"), {"id": run_id}
        ).scalar_one()
    assert status == AnalysisRunStatus.RUNNING.value


def test_queued_run_keeps_its_checkpoints_however_long_ago_it_was_requested(
    sessions, database_url, engine
):
    run_id = run_that_is(AnalysisRunStatus.QUEUED, sessions)
    with transaction(sessions) as session:
        session.execute(
            text("UPDATE analysis_run SET created_at = now() - interval '30 days' WHERE id = :id"),
            {"id": run_id},
        )
    leave_checkpoints(database_url, run_thread_id(run_id))

    clean_up(sessions, longest_run=timedelta(hours=2))

    assert tables_holding(engine, run_thread_id(run_id)) == CHECKPOINT_TABLES_OF_THREADS


def test_checkpoints_of_a_run_that_does_not_exist_are_removed(sessions, database_url, engine):
    thread = run_thread_id(uuid.uuid4())
    leave_checkpoints(database_url, thread)

    clean_up(sessions)

    assert tables_holding(engine, thread) == set()


def test_thread_that_is_not_named_after_a_run_is_removed(sessions, database_url, engine):
    leave_checkpoints(database_url, "not-a-run-id")

    clean_up(sessions)

    assert tables_holding(engine, "not-a-run-id") == set()


def test_cleanup_says_how_many_threads_it_removed(sessions, database_url):
    clean_up(sessions)
    for status in (AnalysisRunStatus.SUCCEEDED, AnalysisRunStatus.FAILED):
        leave_checkpoints(database_url, run_thread_id(run_that_is(status, sessions)))
    leave_checkpoints(database_url, run_thread_id(run_that_is(AnalysisRunStatus.RUNNING, sessions)))

    assert clean_up(sessions) == 2
    assert clean_up(sessions) == 0


def test_cleanup_is_one_statement(sessions, database_url, session, statements):
    leave_checkpoints(database_url, run_thread_id(run_that_is(AnalysisRunStatus.FAILED, sessions)))
    session.connection()
    statements.clear()
    remove_unneeded_checkpoints(session, longest_run=A_DAY)
    assert len(statements) == 1


def test_cleanup_that_is_rolled_back_removes_nothing(sessions, database_url, engine):
    thread = run_thread_id(run_that_is(AnalysisRunStatus.FAILED, sessions))
    leave_checkpoints(database_url, thread)

    with sessions() as session:
        remove_unneeded_checkpoints(session, longest_run=A_DAY)
        session.rollback()

    assert tables_holding(engine, thread) == CHECKPOINT_TABLES_OF_THREADS
