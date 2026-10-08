"""Tests for the activities as plain functions, against PostgreSQL.

They run in Temporal's activity environment, which needs no server.
"""

import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from openmarketer_core.analysis_workflow import AnalyzeRepositoryInput
from openmarketer_core.analyzer import AnalysisError
from openmarketer_core.db.models import AnalysisRunStatus
from openmarketer_core.intake import IntakeError
from openmarketer_core.llm import LLMError
from openmarketer_core.llm_config import ConfigError
from openmarketer_core.repository_analysis import RepositoryAnalysis
from openmarketer_worker import steps
from openmarketer_worker.activities import AnalysisActivities
from openmarketer_worker.policy import AnalysisPolicy

POLICY = AnalysisPolicy(attempt_timeout_seconds=600, max_attempts=3)


@pytest.fixture
def activities(sessions, analysis) -> AnalysisActivities:
    return AnalysisActivities(sessions, analysis, POLICY)


@pytest.fixture
def started(activities, run) -> AnalyzeRepositoryInput:
    ActivityEnvironment().run(activities.start_run, run)
    return run


@pytest.fixture
def failing(sessions, scripted):
    """Activities whose analysis fails with the given errors, one per attempt."""

    def activities(*failures: Exception) -> AnalysisActivities:
        return AnalysisActivities(sessions, scripted(list(failures)), POLICY)

    return activities


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


async def test_of_two_attempts_running_at_once_one_result_is_stored(
    sessions, scripted, started, database
):
    other_attempt = AnalysisActivities(sessions, scripted(), POLICY)
    overtaken = scripted(meanwhile=lambda: other_attempt._analyse_and_store(started))
    await ActivityEnvironment().run(
        AnalysisActivities(sessions, overtaken, POLICY).analyse_and_store, started
    )
    assert database.profile_versions(started) == 1
    assert database.run(started).profile_version == 1


async def test_result_of_a_run_that_failed_meanwhile_is_not_stored(
    activities, sessions, scripted, started, database
):
    gave_up = steps.RunFailure(run=started, error=steps.TIMED_OUT)
    late = scripted(meanwhile=lambda: activities.record_failure(gave_up))
    failure = await refused(AnalysisActivities(sessions, late, POLICY).analyse_and_store, started)
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
    sessions, analysis, database, repository
):
    activities = AnalysisActivities(sessions, analysis, POLICY)
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


async def test_failure_does_not_name_the_clone_folder(sessions, started):
    def analyse(repository_url: str, clone_into: Path) -> RepositoryAnalysis:
        raise IntakeError(f"git clone failed: Cloning into '{clone_into}'... not found")

    failure = await refused(
        AnalysisActivities(sessions, analyse, POLICY).analyse_and_store, started
    )
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
    sessions, scripted, started, database
):
    # PostgreSQL stores no NUL character in a text column.
    unstorable = scripted(ref="main\x00")
    failure = await refused(
        AnalysisActivities(sessions, unstorable, POLICY).analyse_and_store, started
    )
    assert (failure.type, failure.message) == (steps.DATABASE_UNAVAILABLE, steps.NOT_STORED)
    assert database.profile_versions(started) == 0
    assert database.run(started).status is AnalysisRunStatus.RUNNING


@pytest.mark.parametrize("failures", [[], [IntakeError("no")], [RuntimeError("no")]])
async def test_clone_is_removed_whatever_the_outcome(sessions, scripted, started, failures):
    analysis = scripted(list(failures))
    activities = AnalysisActivities(sessions, analysis, POLICY)
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
