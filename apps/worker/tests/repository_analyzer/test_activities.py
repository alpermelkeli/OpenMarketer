"""Tests for the activities as plain functions, against PostgreSQL.

They run in Temporal's activity environment, which needs no server.
"""

import asyncio
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from openmarketer_core.analysis_workflow import AnalyzeRepositoryInput
from openmarketer_core.analyzer import AnalysisError
from openmarketer_core.db.analysis_runs import mark_run_failed
from openmarketer_core.db.models import AnalysisRunStatus
from openmarketer_core.db.session import DatabaseError, session_factory, transaction
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.intake import IntakeError
from openmarketer_core.llm import LLMError
from openmarketer_core.llm_config import ConfigError
from openmarketer_core.repository_analysis import RepositoryAnalysis
from openmarketer_worker.repository_analyzer import steps
from openmarketer_worker.repository_analyzer.activities import AnalysisActivities, CheckpointAccess
from openmarketer_worker.repository_analyzer.checkpoint_cleanup import (
    keep_removing_leftover_checkpoints,
    remove_leftover_checkpoints,
)
from openmarketer_worker.repository_analyzer.policy import AnalysisPolicy

POLICY = AnalysisPolicy(attempt_timeout_seconds=600, max_attempts=3)
A_DAY = timedelta(days=1)


@pytest.fixture
def activities_with(sessions, checkpoints):
    """Activities that analyse with the given callable, and reach checkpoints as given."""

    def activities(analyse, reaching: CheckpointAccess = checkpoints) -> AnalysisActivities:
        return AnalysisActivities(sessions, analyse, reaching, POLICY)

    return activities


@pytest.fixture
def activities(activities_with, analysis) -> AnalysisActivities:
    return activities_with(analysis)


@pytest.fixture
def started(activities, run) -> AnalyzeRepositoryInput:
    ActivityEnvironment().run(activities.start_run, run)
    return run


@pytest.fixture
def failing(activities_with, scripted):
    """Activities whose analysis fails with the given errors, one per attempt."""

    def activities(*failures: Exception) -> AnalysisActivities:
        return activities_with(scripted(list(failures)))

    return activities


def cannot_forget(checkpoints: CheckpointAccess) -> CheckpointAccess:
    """The same checkpoints, with a database that fails whenever a run is to be forgotten."""

    def forget(thread_id: str) -> None:
        raise DatabaseError("connection is closed")

    return replace(checkpoints, forget=forget)


def without_the_hold(checkpoints: CheckpointAccess) -> CheckpointAccess:
    """The same checkpoints, as when no attempt's hold on a run keeps another out."""

    @contextmanager
    def of_attempt(thread_id: str) -> Iterator[RunCheckpoints]:
        with checkpoints.of_attempt(f"{thread_id}-{uuid.uuid4()}") as held_elsewhere:
            yield replace(held_elsewhere, thread_id=thread_id)

    return replace(checkpoints, of_attempt=of_attempt)


def in_another_workspace(run: AnalyzeRepositoryInput, database) -> AnalyzeRepositoryInput:
    return replace(run, workspace_id=database.new_workspace())


async def refused(activity, argument) -> ApplicationError:
    with pytest.raises(ApplicationError) as failure:
        await ActivityEnvironment().run(activity, argument)
    return failure.value


# ------------------------------------------------------------------ start_run
def test_start_marks_the_run_running(activities, run, database):
    ActivityEnvironment().run(activities.start_run, run)
    assert database.run(run).status is AnalysisRunStatus.RUNNING


def test_start_returns_the_limits_this_worker_was_configured_with(activities, run):
    assert ActivityEnvironment().run(activities.start_run, run) == POLICY


def test_start_can_be_repeated(activities, run, database):
    ActivityEnvironment().run(activities.start_run, run)
    first = database.run(run)
    ActivityEnvironment().run(activities.start_run, run)
    assert database.run(run) == first


def test_start_of_a_run_that_is_not_there_yet_is_worth_another_attempt(activities, some_run):
    with pytest.raises(ApplicationError) as failure:
        ActivityEnvironment().run(activities.start_run, some_run())
    assert failure.value.type == steps.RUN_NOT_FOUND_YET
    assert not failure.value.non_retryable


def test_start_from_another_workspace_does_not_touch_the_run(activities, run, database):
    with pytest.raises(ApplicationError):
        ActivityEnvironment().run(activities.start_run, in_another_workspace(run, database))
    assert database.run(run).status is AnalysisRunStatus.QUEUED


def test_start_of_a_finished_run_is_refused_for_good(activities, run, database):
    ActivityEnvironment().run(activities.record_failure, steps.RunFailure(run=run, error="no"))
    with pytest.raises(ApplicationError) as failure:
        ActivityEnvironment().run(activities.start_run, run)
    assert (failure.value.type, failure.value.non_retryable) == (steps.RUN_UNAVAILABLE, True)


# ---------------------------------------------------------- analyse_and_store
async def test_analysis_stores_one_profile_version_and_the_run_succeeds_with_it(
    activities, started, database
):
    await ActivityEnvironment().run(activities.analyse_and_store, started)
    stored = database.run(started)
    assert (stored.status, stored.profile_version) == (AnalysisRunStatus.SUCCEEDED, 1)
    assert database.profile_versions(started) == 1


async def test_analysis_clones_the_repository_of_the_project(activities, analysis, started):
    await ActivityEnvironment().run(activities.analyse_and_store, started)
    assert [url for url, _ in analysis.cloned] == ["https://example.com/acme/app.git"]


async def test_repeated_analysis_neither_analyses_nor_stores_again(
    activities, analysis, started, database
):
    await ActivityEnvironment().run(activities.analyse_and_store, started)
    first = database.run(started)
    await ActivityEnvironment().run(activities.analyse_and_store, started)
    assert len(analysis.cloned) == 1
    assert database.profile_versions(started) == 1
    assert database.run(started) == first


async def test_attempt_is_refused_while_an_earlier_one_is_still_running(
    activities_with, scripted, started
):
    second = scripted()
    refusals: list[ApplicationError] = []

    def second_attempt() -> None:
        with pytest.raises(ApplicationError) as failure:
            activities_with(second)._analyse_and_store(started)
        refusals.append(failure.value)

    await ActivityEnvironment().run(
        activities_with(scripted(meanwhile=second_attempt)).analyse_and_store, started
    )
    (refusal,) = refusals
    assert (refusal.type, refusal.non_retryable) == (steps.ATTEMPT_STILL_RUNNING, False)
    assert refusal.message == steps.EARLIER_ATTEMPT_RUNNING
    assert second.cloned == []


async def test_attempt_refused_for_an_earlier_one_leaves_the_run_to_that_one(
    activities_with, scripted, started, database
):
    def second_attempt() -> None:
        with pytest.raises(ApplicationError):
            activities_with(scripted())._analyse_and_store(started)

    await ActivityEnvironment().run(
        activities_with(scripted(meanwhile=second_attempt)).analyse_and_store, started
    )
    assert database.run(started).profile_version == 1
    assert database.profile_versions(started) == 1


async def test_of_two_attempts_that_both_analysed_one_result_is_stored(
    activities_with, checkpoints, scripted, started, database
):
    unheld = without_the_hold(checkpoints)
    other_attempt = activities_with(scripted(), unheld)
    overtaken = scripted(meanwhile=lambda: other_attempt._analyse_and_store(started))
    await ActivityEnvironment().run(activities_with(overtaken, unheld).analyse_and_store, started)
    assert database.profile_versions(started) == 1
    assert database.run(started).profile_version == 1


async def test_result_of_a_run_that_failed_meanwhile_is_not_stored(
    activities, activities_with, scripted, started, database
):
    gave_up = steps.RunFailure(run=started, error=steps.TIMED_OUT)
    late = scripted(meanwhile=lambda: activities.record_failure(gave_up))
    failure = await refused(activities_with(late).analyse_and_store, started)
    assert (failure.type, failure.non_retryable) == (steps.RUN_UNAVAILABLE, True)
    assert database.profile_versions(started) == 0
    assert database.run(started).error == steps.TIMED_OUT


async def test_analysis_from_another_workspace_touches_nothing(
    activities, analysis, started, database
):
    failure = await refused(activities.analyse_and_store, in_another_workspace(started, database))
    assert (failure.type, failure.non_retryable) == (steps.RUN_UNAVAILABLE, True)
    assert analysis.cloned == []
    assert database.run(started).status is AnalysisRunStatus.RUNNING
    assert database.profile_versions(started) == 0


async def test_analysis_of_another_project_touches_nothing(activities, analysis, started, database):
    other = database.requested_run()
    failure = await refused(
        activities.analyse_and_store, replace(started, project_id=other.project_id)
    )
    assert failure.type == steps.RUN_UNAVAILABLE
    assert analysis.cloned == []
    assert database.profile_versions(other) == 0


@pytest.mark.parametrize("repository", ["file:///etc", "/srv/repositories/app"])
async def test_project_stored_with_a_local_folder_is_not_cloned(
    activities, analysis, database, repository
):
    run = database.requested_run(repository)
    ActivityEnvironment().run(activities.start_run, run)
    failure = await refused(activities.analyse_and_store, run)
    assert (failure.type, failure.non_retryable) == (steps.ANALYSIS_FAILED, True)
    assert analysis.cloned == []


@pytest.mark.parametrize(
    "error",
    [
        IntakeError("git clone failed: repository not found"),
        AnalysisError("cost limit reached ($0.500) before a profile was accepted"),
        ConfigError("unknown role 'repo_analyzer'"),
    ],
)
async def test_refused_repository_or_profile_is_not_worth_another_attempt(failing, started, error):
    failure = await refused(failing(error).analyse_and_store, started)
    assert (failure.type, failure.non_retryable) == (steps.ANALYSIS_FAILED, True)
    assert failure.message == str(error)


async def test_model_provider_failure_is_worth_another_attempt(failing, started):
    limited = LLMError("the model provider answered HTTP 429", status=429)
    failure = await refused(failing(limited).analyse_and_store, started)
    assert (failure.type, failure.non_retryable) == (steps.MODEL_UNAVAILABLE, False)
    assert failure.message == "the model provider answered HTTP 429"


@pytest.mark.parametrize("status", [401, 402, 403, 404])
async def test_request_the_model_provider_refuses_is_not_worth_another_attempt(
    failing, started, status
):
    refusal = LLMError(f"the model provider answered HTTP {status}", status=status)
    failure = await refused(failing(refusal).analyse_and_store, started)
    assert (failure.type, failure.non_retryable) == (steps.ANALYSIS_FAILED, True)


async def test_model_provider_that_did_not_answer_is_worth_another_attempt(failing, started):
    failure = await refused(
        failing(LLMError("request failed: ReadTimeout")).analyse_and_store, started
    )
    assert (failure.type, failure.non_retryable) == (steps.MODEL_UNAVAILABLE, False)


async def test_failed_attempt_leaves_the_run_running_for_the_workflow_to_settle(
    failing, started, database
):
    await refused(
        failing(LLMError("the model provider answered HTTP 503", status=503)).analyse_and_store,
        started,
    )
    assert database.run(started).status is AnalysisRunStatus.RUNNING


async def test_failure_does_not_name_the_clone_folder(activities_with, started):
    def analyse(
        repository_url: str, clone_into: Path, checkpoints: RunCheckpoints
    ) -> RepositoryAnalysis:
        raise IntakeError(f"git clone failed: Cloning into '{clone_into}'... not found")

    failure = await refused(activities_with(analyse).analyse_and_store, started)
    assert failure.message == "git clone failed: Cloning into '<clone>/repo'... not found"


async def test_failure_carries_no_cause_into_the_history(failing, started):
    failure = await refused(failing(IntakeError("git clone failed")).analyse_and_store, started)
    assert failure.__cause__ is None


async def test_unexpected_error_is_reported_without_its_detail(failing, started):
    failure = await refused(
        failing(RuntimeError("/Users/someone/secret")).analyse_and_store, started
    )
    assert (failure.type, failure.message) == (steps.UNEXPECTED, steps.STOPPED_UNEXPECTEDLY)
    assert not failure.non_retryable
    assert failure.__cause__ is None


async def test_result_the_database_refuses_is_reported_without_its_detail(
    activities_with, scripted, started, database
):
    # PostgreSQL stores no NUL character in a text column.
    unstorable = scripted(ref="main\x00")
    failure = await refused(activities_with(unstorable).analyse_and_store, started)
    assert (failure.type, failure.message) == (steps.DATABASE_UNAVAILABLE, steps.NOT_STORED)
    assert database.profile_versions(started) == 0
    assert database.run(started).status is AnalysisRunStatus.RUNNING


@pytest.mark.parametrize("failures", [[], [IntakeError("no")], [RuntimeError("no")]])
async def test_clone_is_removed_whatever_the_outcome(activities_with, scripted, started, failures):
    analysis = scripted(list(failures))
    activities = activities_with(analysis)
    try:
        await ActivityEnvironment().run(activities.analyse_and_store, started)
    except ApplicationError:
        pass
    (_, clone_into), *_ = analysis.cloned
    assert not clone_into.parent.exists()


async def test_analysis_sends_a_heartbeat(activities, started):
    environment = ActivityEnvironment()
    heartbeats: list[object] = []
    environment.on_heartbeat = lambda *details: heartbeats.append(details)
    await environment.run(activities.analyse_and_store, started)
    assert heartbeats


# ------------------------------------------- the run's checkpoints across attempts
OUTAGE = LLMError("the model provider answered HTTP 503", status=503)


async def test_attempt_that_failed_in_a_way_that_may_pass_keeps_the_checkpoints(
    failing, started, database
):
    await refused(failing(OUTAGE).analyse_and_store, started)
    assert database.has_checkpoints(started)


async def test_attempt_whose_profile_was_not_stored_keeps_the_checkpoints(
    activities_with, scripted, started, database
):
    await refused(activities_with(scripted(ref="main\x00")).analyse_and_store, started)
    assert database.has_checkpoints(started)


async def test_run_that_succeeded_leaves_no_checkpoints(activities, started, database):
    await ActivityEnvironment().run(activities.analyse_and_store, started)
    assert not database.has_checkpoints(started)


async def test_run_recorded_failed_leaves_no_checkpoints(failing, started, database):
    activities = failing(OUTAGE)
    await refused(activities.analyse_and_store, started)
    ActivityEnvironment().run(
        activities.record_failure, steps.RunFailure(run=started, error=str(OUTAGE))
    )
    assert not database.has_checkpoints(started)


async def test_refused_analysis_leaves_no_checkpoints_once_its_failure_is_recorded(
    failing, started, database
):
    activities = failing(AnalysisError("no profile after 40 steps"))
    failure = await refused(activities.analyse_and_store, started)
    ActivityEnvironment().run(
        activities.record_failure, steps.RunFailure(run=started, error=failure.message)
    )
    assert not database.has_checkpoints(started)


async def test_attempt_still_running_after_its_run_failed_leaves_no_checkpoints(
    activities, activities_with, scripted, started, database
):
    gave_up = steps.RunFailure(run=started, error=steps.TIMED_OUT)
    # The scripted attempt writes a checkpoint after the failure was recorded.
    late = scripted(meanwhile=lambda: activities.record_failure(gave_up))
    await refused(activities_with(late).analyse_and_store, started)
    assert not database.has_checkpoints(started)


async def test_failure_recorded_from_another_workspace_keeps_the_checkpoints_of_the_run(
    failing, started, database
):
    activities = failing(OUTAGE)
    await refused(activities.analyse_and_store, started)
    elsewhere = in_another_workspace(started, database)
    ActivityEnvironment().run(
        activities.record_failure, steps.RunFailure(run=elsewhere, error="no")
    )
    assert database.has_checkpoints(started)


async def test_run_succeeds_when_its_checkpoints_cannot_be_forgotten(
    activities_with, analysis, checkpoints, started, database
):
    activities = activities_with(analysis, cannot_forget(checkpoints))
    await ActivityEnvironment().run(activities.analyse_and_store, started)
    assert database.run(started).status is AnalysisRunStatus.SUCCEEDED
    assert database.has_checkpoints(started)


async def test_repeated_attempt_at_a_succeeded_run_forgets_what_was_left(
    activities, activities_with, analysis, checkpoints, started, database
):
    died_before_forgetting = activities_with(analysis, cannot_forget(checkpoints))
    await ActivityEnvironment().run(died_before_forgetting.analyse_and_store, started)
    await ActivityEnvironment().run(activities.analyse_and_store, started)
    assert not database.has_checkpoints(started)
    assert len(analysis.cloned) == 1


async def test_failure_is_recorded_when_the_checkpoints_cannot_be_forgotten(
    activities_with, scripted, checkpoints, started, database
):
    activities = activities_with(scripted([OUTAGE]), cannot_forget(checkpoints))
    await refused(activities.analyse_and_store, started)
    ActivityEnvironment().run(
        activities.record_failure, steps.RunFailure(run=started, error=str(OUTAGE))
    )
    stored = database.run(started)
    assert (stored.status, stored.error) == (AnalysisRunStatus.FAILED, str(OUTAGE))
    assert database.has_checkpoints(started)


async def test_cleanup_removes_what_a_succeeded_run_could_not_forget(
    activities_with, analysis, checkpoints, sessions, started, database
):
    activities = activities_with(analysis, cannot_forget(checkpoints))
    await ActivityEnvironment().run(activities.analyse_and_store, started)
    remove_leftover_checkpoints(sessions, longest_run=A_DAY)
    assert not database.has_checkpoints(started)


async def test_cleanup_removes_what_a_failed_run_could_not_forget(
    activities_with, scripted, checkpoints, sessions, started, database
):
    activities = activities_with(scripted([OUTAGE]), cannot_forget(checkpoints))
    await refused(activities.analyse_and_store, started)
    ActivityEnvironment().run(activities.record_failure, steps.RunFailure(run=started, error="no"))
    remove_leftover_checkpoints(sessions, longest_run=A_DAY)
    assert not database.has_checkpoints(started)


async def test_cleanup_keeps_the_checkpoints_of_a_run_that_will_be_tried_again(
    failing, sessions, started, database
):
    await refused(failing(OUTAGE).analyse_and_store, started)
    remove_leftover_checkpoints(sessions, longest_run=A_DAY)
    assert database.has_checkpoints(started)


async def test_cleanup_removes_the_checkpoints_of_a_run_that_outlasted_what_a_run_can_take(
    failing, sessions, started, database
):
    await refused(failing(OUTAGE).analyse_and_store, started)
    remove_leftover_checkpoints(sessions, longest_run=timedelta(0))
    assert not database.has_checkpoints(started)
    assert database.run(started).status is AnalysisRunStatus.RUNNING


def test_cleanup_that_cannot_reach_the_database_raises_nothing(caplog):
    unreachable = session_factory(
        "postgresql+psycopg://nobody:secret@127.0.0.1:1/nothing?connect_timeout=2"
    )
    remove_leftover_checkpoints(unreachable, longest_run=A_DAY)
    assert "left-over checkpoints were not removed" in caplog.text
    assert "secret" not in caplog.text


async def test_cleanup_runs_at_once_and_again_after_the_interval(
    failing, sessions, started, database
):
    activities = failing(OUTAGE)
    cleaning = asyncio.create_task(
        keep_removing_leftover_checkpoints(sessions, every_seconds=0.05, longest_run=A_DAY)
    )
    try:
        await refused(activities.analyse_and_store, started)
        with transaction(sessions) as session:
            mark_run_failed(
                session,
                workspace_id=started.workspace_id,
                project_id=started.project_id,
                run_id=started.run_id,
                error="no",
            )
        await asyncio.wait_for(database.checkpoints_gone(started), timeout=10)
    finally:
        cleaning.cancel()
        await asyncio.gather(cleaning, return_exceptions=True)
    assert not database.has_checkpoints(started)


async def test_attempt_logs_how_it_began_with_the_run_and_nothing_else(activities, started, caplog):
    with caplog.at_level(logging.INFO, logger="openmarketer_worker.repository_analyzer.activities"):
        await ActivityEnvironment().run(activities.analyse_and_store, started)
    assert f"analysis run {started.run_id}: this attempt began fresh" in caplog.messages


# ------------------------------------------------------------- record_failure
def test_failure_is_recorded_on_the_run(activities, started, database):
    failed = steps.RunFailure(run=started, error="git clone failed: not found")
    ActivityEnvironment().run(activities.record_failure, failed)
    stored = database.run(started)
    assert (stored.status, stored.error) == (
        AnalysisRunStatus.FAILED,
        "git clone failed: not found",
    )


def test_run_that_never_started_can_be_failed(activities, run, database):
    ActivityEnvironment().run(activities.record_failure, steps.RunFailure(run=run, error="no"))
    assert database.run(run).status is AnalysisRunStatus.FAILED


def test_recording_a_failure_twice_keeps_the_first_reason(activities, started, database):
    for reason in ("first", "second"):
        ActivityEnvironment().run(
            activities.record_failure, steps.RunFailure(run=started, error=reason)
        )
    assert database.run(started).error == "first"


async def test_failure_of_a_run_that_succeeded_is_not_recorded(activities, started, database):
    await ActivityEnvironment().run(activities.analyse_and_store, started)
    ActivityEnvironment().run(activities.record_failure, steps.RunFailure(run=started, error="no"))
    assert database.run(started).status is AnalysisRunStatus.SUCCEEDED


def test_failure_of_a_run_in_another_workspace_is_not_recorded(activities, started, database):
    elsewhere = in_another_workspace(started, database)
    ActivityEnvironment().run(
        activities.record_failure, steps.RunFailure(run=elsewhere, error="no")
    )
    assert database.run(started).status is AnalysisRunStatus.RUNNING


def test_failure_of_an_unknown_run_is_nothing_to_record(activities):
    unknown = AnalyzeRepositoryInput(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    ActivityEnvironment().run(activities.record_failure, steps.RunFailure(run=unknown, error="no"))
