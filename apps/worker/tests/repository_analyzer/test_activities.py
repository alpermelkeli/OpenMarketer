"""Tests for the two activities of the workflow's hooks, as plain functions against PostgreSQL.

They run in Temporal's activity environment, which needs no server. What the runtime's
activities do for a run of an analysis is in ``test_analysis.py``; here they only leave the
checkpoints the cleanup's tests need.
"""

import asyncio
import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from temporalio.exceptions import ApplicationError

from openmarketer_core.db.analysis_runs import mark_run_failed
from openmarketer_core.db.checkpoint_cleanup import remove_checkpoints_of_analysis_runs
from openmarketer_core.db.models import AnalysisRunStatus
from openmarketer_core.db.session import DatabaseError, session_factory, transaction
from openmarketer_core.llm import LLMError
from openmarketer_core.repository_analysis.workflow_contract import AnalyzeRepositoryInput
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess
from openmarketer_worker.checkpoint_cleanup import (
    CheckpointCleanup,
    keep_removing_leftover_checkpoints,
    remove_leftover_checkpoints,
)
from openmarketer_worker.repository_analyzer import steps

A_DAY = timedelta(days=1)
OUTAGE = LLMError("the model provider answered HTTP 503", status=503)


def of_analysis_runs(lasting: timedelta) -> CheckpointCleanup:
    """Core's rule for analysis runs, as the worker's schedule is handed it."""
    return CheckpointCleanup(
        runs="analysis runs",
        remove=lambda session: remove_checkpoints_of_analysis_runs(session, longest_run=lasting),
    )


def cannot_forget(checkpoints: CheckpointAccess) -> CheckpointAccess:
    """The same checkpoints, with a database that fails whenever a run is to be forgotten."""

    def forget(thread_id: str) -> None:
        raise DatabaseError("connection is closed")

    return replace(checkpoints, forget=forget)


def in_another_workspace(run: AnalyzeRepositoryInput, database) -> AnalyzeRepositoryInput:
    return replace(run, workspace_id=database.new_workspace())


@pytest.fixture
def interrupted(analyzer, model, started):
    """A run whose attempt lost the model provider, and so keeps its checkpoints."""

    async def interrupted() -> AnalyzeRepositoryInput:
        model.turns.append(OUTAGE)
        with pytest.raises(ApplicationError):
            await analyzer.to_the_end(started)
        return started

    return interrupted


# ------------------------------------------------------------------ start_run
def test_start_marks_the_run_running(analyzer, run, database):
    analyzer.start(run)
    assert database.run(run).status is AnalysisRunStatus.RUNNING


def test_start_returns_the_limits_this_worker_was_configured_with(analyzer, run):
    assert analyzer.start(run) == analyzer.parts.policy


def test_start_can_be_repeated(analyzer, run, database):
    analyzer.start(run)
    first = database.run(run)
    analyzer.start(run)
    assert database.run(run) == first


def test_start_of_a_run_that_is_not_there_yet_is_worth_another_attempt(analyzer, some_run):
    with pytest.raises(ApplicationError) as failure:
        analyzer.start(some_run())
    assert failure.value.type == steps.RUN_NOT_FOUND_YET
    assert not failure.value.non_retryable


def test_start_from_another_workspace_does_not_touch_the_run(analyzer, run, database):
    with pytest.raises(ApplicationError):
        analyzer.start(in_another_workspace(run, database))
    assert database.run(run).status is AnalysisRunStatus.QUEUED


def test_start_of_a_finished_run_is_refused_for_good(analyzer, run):
    analyzer.record_failure(run, "no")
    with pytest.raises(ApplicationError) as failure:
        analyzer.start(run)
    assert (failure.value.type, failure.value.non_retryable) == (steps.RUN_UNAVAILABLE, True)


# ------------------------------------------------------------- record_failure
def test_failure_is_recorded_on_the_run(analyzer, started, database):
    analyzer.record_failure(started, "git clone failed: not found")
    stored = database.run(started)
    assert (stored.status, stored.error) == (
        AnalysisRunStatus.FAILED,
        "git clone failed: not found",
    )


def test_run_that_never_started_can_be_failed(analyzer, run, database):
    analyzer.record_failure(run, "no")
    assert database.run(run).status is AnalysisRunStatus.FAILED


def test_recording_a_failure_twice_keeps_the_first_reason(analyzer, started, database):
    for reason in ("first", "second"):
        analyzer.record_failure(started, reason)
    assert database.run(started).error == "first"


async def test_failure_of_a_run_that_succeeded_is_not_recorded(analyzer, started, database):
    await analyzer.to_the_end(started)
    analyzer.record_failure(started, "no")
    assert database.run(started).status is AnalysisRunStatus.SUCCEEDED


def test_failure_of_a_run_in_another_workspace_is_not_recorded(analyzer, started, database):
    analyzer.record_failure(in_another_workspace(started, database), "no")
    assert database.run(started).status is AnalysisRunStatus.RUNNING


def test_failure_of_an_unknown_run_is_nothing_to_record(analyzer):
    unknown = AnalyzeRepositoryInput(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    analyzer.record_failure(unknown, "no")


async def test_recording_a_failure_leaves_the_checkpoints_to_the_forgetting_of_the_run(
    analyzer, interrupted, database
):
    run = await interrupted()
    analyzer.record_failure(run, str(OUTAGE))
    assert database.has_checkpoints(run)
    await analyzer.forget(run)
    assert not database.has_checkpoints(run)


# ------------------------------------------------- what the forgetting missed
async def test_cleanup_removes_what_a_succeeded_run_could_not_forget(
    analyzer_with, checkpoints, sessions, started, database
):
    analyzer = analyzer_with(reaching=cannot_forget(checkpoints))
    await analyzer.to_the_end(started)
    await analyzer.forget(started)
    remove_leftover_checkpoints(sessions, [of_analysis_runs(lasting=A_DAY)])
    assert not database.has_checkpoints(started)


async def test_cleanup_removes_what_a_failed_run_could_not_forget(
    analyzer_with, checkpoints, model, sessions, started, database
):
    analyzer = analyzer_with(reaching=cannot_forget(checkpoints))
    model.turns.append(OUTAGE)
    with pytest.raises(ApplicationError):
        await analyzer.to_the_end(started)
    analyzer.record_failure(started, "no")
    await analyzer.forget(started)
    remove_leftover_checkpoints(sessions, [of_analysis_runs(lasting=A_DAY)])
    assert not database.has_checkpoints(started)


async def test_cleanup_keeps_the_checkpoints_of_a_run_that_will_be_tried_again(
    interrupted, sessions, database
):
    run = await interrupted()
    remove_leftover_checkpoints(sessions, [of_analysis_runs(lasting=A_DAY)])
    assert database.has_checkpoints(run)


async def test_cleanup_removes_the_checkpoints_of_a_run_that_outlasted_what_a_run_can_take(
    interrupted, sessions, database
):
    run = await interrupted()
    remove_leftover_checkpoints(sessions, [of_analysis_runs(lasting=timedelta(0))])
    assert not database.has_checkpoints(run)
    assert database.run(run).status is AnalysisRunStatus.RUNNING


def test_cleanup_that_cannot_reach_the_database_raises_nothing(caplog):
    unreachable = session_factory(
        "postgresql+psycopg://nobody:secret@127.0.0.1:1/nothing?connect_timeout=2"
    )
    remove_leftover_checkpoints(unreachable, [of_analysis_runs(lasting=A_DAY)])
    assert "left-over checkpoints of analysis runs were not removed" in caplog.text
    assert "secret" not in caplog.text


async def test_cleanup_runs_at_once_and_again_after_the_interval(interrupted, sessions, database):
    cleaning = asyncio.create_task(
        keep_removing_leftover_checkpoints(
            sessions, [of_analysis_runs(lasting=A_DAY)], every_seconds=0.05
        )
    )
    try:
        run = await interrupted()
        with transaction(sessions) as session:
            mark_run_failed(
                session,
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                run_id=run.run_id,
                error="no",
            )
        await asyncio.wait_for(database.checkpoints_gone(run), timeout=10)
    finally:
        cleaning.cancel()
        await asyncio.gather(cleaning, return_exceptions=True)
    assert not database.has_checkpoints(run)
