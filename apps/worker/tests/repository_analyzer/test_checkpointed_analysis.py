"""Tests for the activities with the real analysis: a retried attempt continues the run.

Git, the secret scan, the analyzer graph and the checkpoint store in
PostgreSQL are real; the model is scripted and the clone is made from a local
repository (see ``AnalysisOfALocalRepository`` in ``conftest.py`` of the worker's tests). They need
gitleaks on the PATH.
"""

import logging
from dataclasses import replace

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from openmarketer_core.db.models import AnalysisRunStatus
from openmarketer_core.llm import LLMError
from openmarketer_core.repository_analysis.analyzer_agent import Resumption
from openmarketer_core.repository_analysis.pipeline import RepositoryAnalysis
from openmarketer_core.repository_analysis.workflow_contract import AnalyzeRepositoryInput
from openmarketer_worker.repository_analyzer import steps
from openmarketer_worker.repository_analyzer.activities import AnalysisActivities
from openmarketer_worker.repository_analyzer.policy import AnalysisPolicy

POLICY = AnalysisPolicy(attempt_timeout_seconds=600)
OUTAGE = LLMError("the model provider answered HTTP 503", status=503)


@pytest.fixture
def activities_with(sessions, checkpoints):
    def activities(analyse) -> AnalysisActivities:
        return AnalysisActivities(sessions, analyse, checkpoints, POLICY)

    return activities


@pytest.fixture
def started(activities_with, analysis, run) -> AnalyzeRepositoryInput:
    ActivityEnvironment().run(activities_with(analysis).start_run, run)
    return run


@pytest.fixture
def interrupted(activities_with, analysis_of_a_local_repository, turns, started):
    """An analysis whose first attempt read the README and then lost the model provider."""

    async def interrupted():
        analysis = analysis_of_a_local_repository(turns.reads_the_readme, OUTAGE, turns.submits)
        with pytest.raises(ApplicationError) as failure:
            await ActivityEnvironment().run(activities_with(analysis).analyse_and_store, started)
        assert failure.value.type == steps.MODEL_UNAVAILABLE
        return analysis

    return interrupted


def unstorable(result: RepositoryAnalysis) -> RepositoryAnalysis:
    # PostgreSQL stores no NUL character in a text column.
    snapshot = replace(result.intake.snapshot, ref="main\x00")
    return replace(result, intake=replace(result.intake, snapshot=snapshot))


async def test_interrupted_attempt_keeps_the_checkpoints_of_its_finished_turns(
    interrupted, started, database
):
    await interrupted()
    assert database.has_checkpoints(started)


async def test_next_attempt_asks_the_model_only_for_the_turn_that_did_not_finish(
    interrupted, activities_with, started
):
    analysis = await interrupted()
    await ActivityEnvironment().run(activities_with(analysis).analyse_and_store, started)
    first_turn, lost_turn, repeated_turn = analysis.requests
    assert repeated_turn == lost_turn
    assert len(repeated_turn) > len(first_turn)


async def test_next_attempt_continues_with_what_the_first_one_read(
    interrupted, activities_with, started
):
    analysis = await interrupted()
    await ActivityEnvironment().run(activities_with(analysis).analyse_and_store, started)
    read_by_the_first_attempt = [m for m in analysis.requests[-1] if m["role"] == "tool"]
    assert "Share memories offline." in read_by_the_first_attempt[0]["content"]
    assert analysis.results[-1].analysis.resumption is Resumption.CONTINUED


async def test_continued_run_succeeds_and_leaves_no_checkpoints(
    interrupted, activities_with, started, database
):
    analysis = await interrupted()
    await ActivityEnvironment().run(activities_with(analysis).analyse_and_store, started)
    stored = database.run(started)
    assert (stored.status, stored.profile_version) == (AnalysisRunStatus.SUCCEEDED, 1)
    assert not database.has_checkpoints(started)


async def test_continued_attempt_is_logged_as_continued(
    interrupted, activities_with, started, caplog
):
    analysis = await interrupted()
    with caplog.at_level(logging.INFO, logger="openmarketer_worker.repository_analyzer.activities"):
        await ActivityEnvironment().run(activities_with(analysis).analyse_and_store, started)
    assert f"analysis run {started.run_id}: this attempt began continued" in caplog.messages


async def test_profile_that_was_not_stored_is_stored_by_the_next_attempt_without_the_model(
    activities_with, analysis_of_a_local_repository, turns, started, database
):
    analysis = analysis_of_a_local_repository(turns.submits)
    spoiled = iter([unstorable])

    def analyse(repository_url, clone_into, checkpoints) -> RepositoryAnalysis:
        result = analysis(repository_url, clone_into, checkpoints)
        return next(spoiled, lambda stored_as_it_is: stored_as_it_is)(result)

    activities = activities_with(analyse)
    with pytest.raises(ApplicationError) as failure:
        await ActivityEnvironment().run(activities.analyse_and_store, started)
    await ActivityEnvironment().run(activities.analyse_and_store, started)

    assert failure.value.message == steps.NOT_STORED
    assert len(analysis.requests) == 1
    assert analysis.results[-1].analysis.resumption is Resumption.ALREADY_FINISHED
    assert database.run(started).status is AnalysisRunStatus.SUCCEEDED
    assert database.profile_versions(started) == 1
    assert not database.has_checkpoints(started)
