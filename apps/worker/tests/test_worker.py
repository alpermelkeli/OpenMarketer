"""Tests for the worker process: runs from end to end, and how it starts and serves.

A run goes through Temporal (the dev stack's server, see ``conftest.py``) and
PostgreSQL on a worker built the way the process builds it; only cloning and
the model are scripted. The start-up failures are seen through ``serve`` and
need neither. How an agent is wired is tested in the agent's folder.
"""

import asyncio
import base64
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest
from google.protobuf.json_format import MessageToDict
from temporalio.client import WorkflowFailureError, WorkflowHistory
from temporalio.worker import Worker

from openmarketer_core.db.analysis_runs import run_thread_id
from openmarketer_core.db.models import AnalysisRunStatus
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.llm import LLMError
from openmarketer_core.repository_analysis.analyzer_agent import AnalysisError
from openmarketer_core.repository_analysis.intake import IntakeError
from openmarketer_core.repository_analysis.pipeline import RepositoryAnalysis
from openmarketer_core.repository_analysis.workflow_contract import (
    ANALYZE_REPOSITORY_WORKFLOW,
    AnalyzeRepositoryInput,
    analysis_workflow_id,
)
from openmarketer_worker import main
from openmarketer_worker.repository_analyzer import wiring
from openmarketer_worker.repository_analyzer.activities import AnalysisActivities
from openmarketer_worker.repository_analyzer.policy import AnalysisPolicy

QUICK = AnalysisPolicy(max_attempts=2, retry_after_seconds=1)
MODELS = str(Path(__file__).resolve().parents[3] / "config" / "models.yaml")
MODEL = {"LLM_CONFIG_PATH": MODELS, "OPENROUTER_API_KEY": "not-a-real-key"}
DATABASE = {"DATABASE_URL": "postgresql+psycopg://u:p@localhost:1/db"}
PROFILE_NAME = "Example App"  # the product name in the profile of conftest.py


def readable(history: WorkflowHistory) -> str:
    """The history as the Temporal UI shows it: payloads are stored encoded, so decode them."""
    document = json.dumps([MessageToDict(event) for event in history.events])
    payloads = [
        base64.b64decode(data).decode(errors="replace")
        for data in re.findall(r'"data": "([A-Za-z0-9+/=]*)"', document)
    ]
    return "\n".join([document, *payloads])


@pytest.fixture
def worker_serving(temporal, task_queue, sessions, checkpoints):
    """A worker built the way the process builds it, on the test's own queue."""

    def worker(analyse) -> Worker:
        activities = AnalysisActivities(sessions, analyse, checkpoints, QUICK)
        agent = replace(wiring.registration(activities, QUICK), task_queue=task_queue)
        return main.build_worker(temporal, agent, max_concurrent_activities=2)

    return worker


@pytest.fixture
def execute(temporal, task_queue, worker_serving):
    """Run the workflow of a run on such a worker."""

    async def execute(run: AnalyzeRepositoryInput, analyse) -> str:
        """Returns everything the workflow history holds, as text."""
        async with worker_serving(analyse):
            handle = await temporal.start_workflow(
                ANALYZE_REPOSITORY_WORKFLOW,
                run,
                id=analysis_workflow_id(run.run_id),
                task_queue=task_queue,
            )
            try:
                await handle.result()
            except WorkflowFailureError:
                pass
            return readable(await handle.fetch_history())

    return execute


async def test_run_ends_succeeded_with_the_one_profile_version_it_stored(
    execute, analysis, run, database
):
    await execute(run, analysis)
    stored = database.run(run)
    assert (stored.status, stored.profile_version, stored.error) == (
        AnalysisRunStatus.SUCCEEDED,
        1,
        None,
    )
    assert database.profile_versions(run) == 1


async def test_refused_repository_ends_the_run_failed_with_a_reason_fit_to_show(
    execute, run, database
):
    attempts: list[Path] = []

    def analyse(
        repository_url: str, clone_into: Path, checkpoints: RunCheckpoints
    ) -> RepositoryAnalysis:
        attempts.append(clone_into)
        raise IntakeError(f"git clone failed: Cloning into '{clone_into}'... not found")

    await execute(run, analyse)
    stored = database.run(run)
    assert (stored.status, stored.error) == (
        AnalysisRunStatus.FAILED,
        "git clone failed: Cloning into '<clone>/repo'... not found",
    )
    assert len(attempts) == 1
    assert database.profile_versions(run) == 0


async def test_model_provider_failure_is_tried_again_and_the_run_succeeds(
    execute, scripted, run, database
):
    analysis = scripted([LLMError("the model provider answered HTTP 429", status=429)])
    await execute(run, analysis)
    assert len(analysis.cloned) == 2
    assert database.run(run).status is AnalysisRunStatus.SUCCEEDED
    assert database.profile_versions(run) == 1


async def test_request_the_model_provider_refuses_ends_the_run_at_the_first_attempt(
    execute, scripted, run, database
):
    analysis = scripted([LLMError("the model provider answered HTTP 401", status=401)])
    await execute(run, analysis)
    stored = database.run(run)
    assert (stored.status, stored.error) == (
        AnalysisRunStatus.FAILED,
        "the model provider answered HTTP 401",
    )
    assert len(analysis.cloned) == 1


async def test_unexpected_error_ends_the_run_failed_without_its_detail(
    execute, scripted, run, database
):
    await execute(run, scripted([RuntimeError("/Users/someone/secret")] * QUICK.max_attempts))
    stored = database.run(run)
    assert (stored.status, stored.error) == (
        AnalysisRunStatus.FAILED,
        "the analysis stopped unexpectedly",
    )


async def test_run_of_a_project_in_another_workspace_is_not_executed(
    execute, analysis, run, database
):
    await execute(replace(run, workspace_id=database.new_workspace()), analysis)
    assert analysis.cloned == []
    assert database.run(run).status is AnalysisRunStatus.QUEUED
    assert database.profile_versions(run) == 0


async def test_run_that_succeeded_after_a_failed_attempt_leaves_no_checkpoints(
    execute, scripted, run, database
):
    await execute(run, scripted([LLMError("the model provider answered HTTP 429", status=429)]))
    assert database.run(run).status is AnalysisRunStatus.SUCCEEDED
    assert not database.has_checkpoints(run)


async def test_run_that_used_up_its_attempts_leaves_no_checkpoints(
    execute, scripted, run, database
):
    outage = LLMError("the model provider answered HTTP 503", status=503)
    await execute(run, scripted([outage] * QUICK.max_attempts))
    assert database.run(run).status is AnalysisRunStatus.FAILED
    assert not database.has_checkpoints(run)


async def test_refused_analysis_leaves_no_checkpoints(execute, scripted, run, database):
    await execute(run, scripted([AnalysisError("no profile after 40 steps")]))
    assert database.run(run).status is AnalysisRunStatus.FAILED
    assert not database.has_checkpoints(run)


async def test_retried_attempt_continues_the_analysis_of_the_one_before(
    execute, analysis_of_a_local_repository, turns, run, database
):
    limited = LLMError("the model provider answered HTTP 429", status=429)
    analysis = analysis_of_a_local_repository(turns.reads_the_readme, limited, turns.submits)
    await execute(run, analysis)
    assert database.run(run).status is AnalysisRunStatus.SUCCEEDED
    assert len(analysis.requests) == 3
    assert not database.has_checkpoints(run)


async def test_history_of_a_continued_run_holds_nothing_the_analyzer_read(
    execute, analysis_of_a_local_repository, turns, run
):
    limited = LLMError("the model provider answered HTTP 429", status=429)
    analysis = analysis_of_a_local_repository(turns.reads_the_readme, limited, turns.submits)
    history = await execute(run, analysis)
    assert str(run.run_id) in history
    assert "Share memories offline." not in history
    assert PROFILE_NAME not in history


async def test_history_of_a_succeeded_run_holds_its_identifiers_and_no_profile(
    execute, analysis, run
):
    history = await execute(run, analysis)
    assert str(run.run_id) in history
    assert PROFILE_NAME not in history
    assert "example.com" not in history


async def test_history_of_a_failed_run_does_not_name_the_clone_folder(execute, run):
    clones: list[Path] = []

    def analyse(
        repository_url: str, clone_into: Path, checkpoints: RunCheckpoints
    ) -> RepositoryAnalysis:
        clones.append(clone_into)
        raise IntakeError(f"git clone failed: Cloning into '{clone_into}'... not found")

    history = await execute(run, analyse)
    assert "Cloning into '<clone>/repo'" in history
    assert str(clones[0].parent) not in history
    assert str(clones[0].parent.resolve()) not in history


# ---------------------------------------------------------------- the process
async def test_worker_does_not_start_without_a_database_url():
    with pytest.raises(main.StartupError, match="DATABASE_URL"):
        await main.serve(MODEL)


async def test_worker_does_not_start_with_a_database_option_the_driver_refuses():
    url = "postgresql+psycopg://app:hunter2@localhost:1/db?no_such_option=1"
    with pytest.raises(main.StartupError, match="invalid database URL") as refused:
        await main.serve({**MODEL, "DATABASE_URL": url})
    assert "hunter2" not in str(refused.value)


async def test_worker_does_not_start_with_a_database_that_cannot_keep_checkpoints():
    with pytest.raises(main.StartupError, match="checkpoints need a PostgreSQL database"):
        await main.serve({**MODEL, "DATABASE_URL": "sqlite://"})


async def test_worker_removes_leftover_checkpoints_when_it_starts(
    worker_serving, sessions, checkpoints, analysis, some_run, database, tmp_path
):
    never_recorded = some_run()
    with checkpoints.of_attempt(run_thread_id(never_recorded.run_id)) as left_behind:
        analysis("https://example.com/acme/app.git", tmp_path / "repo", left_behind)
    stop = asyncio.Event()
    serving = asyncio.create_task(
        main.serve_until(
            stop, worker_serving(analysis), sessions, [wiring.cleanup_of_analysis_runs(QUICK)]
        )
    )
    try:
        await asyncio.wait_for(database.checkpoints_gone(never_recorded), timeout=10)
    finally:
        stop.set()
        await serving
    assert not database.has_checkpoints(never_recorded)


@pytest.mark.parametrize("name", ["WORKER_MAX_CONCURRENT_ACTIVITIES", "ANALYSIS_MAX_ATTEMPTS"])
async def test_worker_does_not_start_with_a_limit_that_is_not_a_number(name):
    with pytest.raises(main.StartupError, match=name):
        await main.serve({**DATABASE, **MODEL, name: "soon"})


async def test_worker_does_not_start_with_a_token_host_that_is_not_a_host():
    environ = {**DATABASE, **MODEL, "GITLAB_HOST": "https://gitlab.example.com"}
    with pytest.raises(main.StartupError, match="GITLAB_HOST"):
        await main.serve(environ)


async def test_worker_does_not_start_without_the_key_of_the_model_provider():
    with pytest.raises(main.StartupError, match="OPENROUTER_API_KEY"):
        await main.serve({**DATABASE, "LLM_CONFIG_PATH": MODELS})


async def test_worker_does_not_start_without_its_model_configuration(tmp_path):
    with pytest.raises(main.StartupError):
        await main.serve({**DATABASE, "LLM_CONFIG_PATH": str(tmp_path / "missing.yaml")})


async def test_unreachable_temporal_is_reported_with_the_setting_to_change():
    environ = {**DATABASE, **MODEL, "TEMPORAL_ADDRESS": "127.0.0.1:1"}
    with pytest.raises(main.StartupError, match=r"127\.0\.0\.1:1 \(TEMPORAL_ADDRESS\)"):
        await main.serve(environ)
