"""Tests for removing the checkpoints that studio runs no longer need.

They need PostgreSQL (see the root ``conftest.py``). Runs and checkpoints are committed, the
way the worker and the checkpoint store write them. The rule is built like the analysis
runs' (``test_checkpoint_cleanup.py``); these tests hold what is the studio's own: its prefix,
and that a run that needs review has finished.
"""

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import TypedDict

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.analysis_runs import run_thread_id as analysis_thread_id
from openmarketer_core.db.checkpoint_cleanup import remove_checkpoints_of_studio_runs
from openmarketer_core.db.checkpoint_schema import CHECKPOINT_TABLES_OF_THREADS
from openmarketer_core.db.media_assets import NewAssetVersion, add_version
from openmarketer_core.db.models import AssetTechnique, Project, StudioRunStatus, Workspace
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.db.studio_runs import (
    mark_run_failed,
    mark_run_needs_review,
    mark_run_started,
    request_studio_run,
    run_thread_id,
)
from openmarketer_core.graph_checkpoints.postgres import run_checkpoints
from openmarketer_core.studio.asset_store import asset_key
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.requests import AssetRequest

A_DAY = timedelta(days=1)
REQUEST = AssetRequest(goal="Announce the launch", platform=Platform.X_POST)


class ProjectIds(TypedDict):
    workspace_id: uuid.UUID
    project_id: uuid.UUID


@pytest.fixture
def database_url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


@pytest.fixture
def sessions(database_url) -> sessionmaker[Session]:
    return session_factory(database_url)


def run_that_is(status: StudioRunStatus, sessions: sessionmaker[Session]) -> uuid.UUID:
    """A studio run of a new project, taken as far as ``status``."""
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
        run_id = request_studio_run(session, **scope, request=REQUEST, requested_by="person").id
        if status is StudioRunStatus.QUEUED:
            return run_id
        mark_run_started(session, **scope, run_id=run_id)
        if status is StudioRunStatus.FAILED:
            mark_run_failed(session, **scope, run_id=run_id, error="no")
        if status is StudioRunStatus.NEEDS_REVIEW:
            version_id = uuid.uuid4()
            add_version(
                session,
                **scope,
                run_id=run_id,
                version_id=version_id,
                action_id=f"studio-run:{run_id}:1",
                version=NewAssetVersion(
                    round=1,
                    technique=AssetTechnique.GENERATED,
                    storage_key=asset_key(
                        **scope, run_id=run_id, version_id=version_id, mime="image/png"
                    ),
                    mime="image/png",
                    width=1600,
                    height=900,
                    size_bytes=10,
                    cost_usd=Decimal(0),
                ),
            )
            mark_run_needs_review(
                session, **scope, run_id=run_id, final_version_id=version_id, cost_usd=Decimal(0)
            )
        return run_id


def leave_checkpoints(database_url: str, thread_id: str) -> None:
    """Rows in all three tables of the thread, as a step of the graph leaves them."""
    with run_checkpoints(database_url, thread_id) as run:
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"messages": ["a brief"]}
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
        return remove_checkpoints_of_studio_runs(session, longest_run=longest_run)


@pytest.mark.parametrize("status", [StudioRunStatus.NEEDS_REVIEW, StudioRunStatus.FAILED])
def test_checkpoints_of_a_finished_run_are_removed(status, sessions, database_url, engine):
    thread = run_thread_id(run_that_is(status, sessions))
    leave_checkpoints(database_url, thread)

    clean_up(sessions)

    assert tables_holding(engine, thread) == set()


@pytest.mark.parametrize("status", [StudioRunStatus.QUEUED, StudioRunStatus.RUNNING])
def test_checkpoints_of_an_unfinished_run_are_kept(status, sessions, database_url, engine):
    thread = run_thread_id(run_that_is(status, sessions))
    leave_checkpoints(database_url, thread)

    clean_up(sessions)

    assert tables_holding(engine, thread) == CHECKPOINT_TABLES_OF_THREADS


def test_a_running_run_started_longer_ago_than_a_run_can_take_loses_them(
    sessions, database_url, engine
):
    run_id = run_that_is(StudioRunStatus.RUNNING, sessions)
    thread = run_thread_id(run_id)
    leave_checkpoints(database_url, thread)
    with transaction(sessions) as session:
        session.execute(
            text("UPDATE studio_run SET started_at = now() - interval '2 days' WHERE id = :id"),
            {"id": run_id},
        )

    clean_up(sessions)

    assert tables_holding(engine, thread) == set()


def test_checkpoints_of_a_studio_run_that_does_not_exist_are_removed(
    sessions, database_url, engine
):
    thread = run_thread_id(uuid.uuid4())
    leave_checkpoints(database_url, thread)

    assert clean_up(sessions) >= 1
    assert tables_holding(engine, thread) == set()


def test_a_thread_of_another_kind_is_not_this_rules(sessions, database_url, engine):
    thread = analysis_thread_id(uuid.uuid4())
    leave_checkpoints(database_url, thread)

    clean_up(sessions)

    assert tables_holding(engine, thread) == CHECKPOINT_TABLES_OF_THREADS
