"""Tests for the worker process: runs from end to end, and how it starts and serves.

A run goes through Temporal (the dev stack's server, see ``conftest.py``) and
PostgreSQL on a worker built the way the process builds it, with the analyzer's
real workflow, activities, graph and clones; only the model and, in most tests,
the clone itself are scripted. The start-up failures are seen through ``serve``
and need neither. How an agent is wired is tested in the agent's folder.
"""

import asyncio
import base64
import json
import re
import threading
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from google.protobuf.json_format import MessageToDict
from temporalio.client import WorkflowFailureError, WorkflowHistory
from temporalio.exceptions import CancelledError
from temporalio.worker import Worker

from openmarketer_core.credentials.catalog import GITHUB_TOKEN, OPENROUTER_API_KEY
from openmarketer_core.db.analysis_runs import run_thread_id
from openmarketer_core.db.models import AnalysisRunStatus
from openmarketer_core.llm import LLMError
from openmarketer_core.llm_config import ModelRouter
from openmarketer_core.repository_analysis.analyzer_agent import AnalysisError
from openmarketer_core.repository_analysis.intake import IntakeError
from openmarketer_core.repository_analysis.workflow_contract import (
    ANALYZE_REPOSITORY_WORKFLOW,
    AnalyzeRepositoryInput,
    analysis_workflow_id,
)
from openmarketer_worker import main
from openmarketer_worker.credentials import ModelKeyNotSet, WorkspaceCredentials
from openmarketer_worker.repository_analyzer import wiring
from openmarketer_worker.repository_analyzer.policy import AnalysisPolicy
from openmarketer_worker.repository_analyzer.repository_tokens import WorkspaceRepositoryTokens

QUICK = AnalysisPolicy(max_attempts=2, retry_after_seconds=1)
MODELS = str(Path(__file__).resolve().parents[3] / "config" / "models.yaml")
MODEL = {"LLM_CONFIG_PATH": MODELS, "OPENROUTER_API_KEY": "not-a-real-key"}
DATABASE = {"DATABASE_URL": "postgresql+psycopg://u:p@localhost:1/db"}
PROFILE_NAME = "Example App"  # the product name in the profile of conftest.py
RATE_LIMITED = LLMError("the model provider answered HTTP 429", status=429)


def readable(history: WorkflowHistory) -> str:
    """The history as the Temporal UI shows it: payloads are stored encoded, so decode them."""
    document = json.dumps([MessageToDict(event) for event in history.events])
    payloads = [
        base64.b64decode(data).decode(errors="replace")
        for data in re.findall(r'"data": "([A-Za-z0-9+/=]*)"', document)
    ]
    return "\n".join([document, *payloads])


def not_found_in(folder: Path) -> IntakeError:
    return IntakeError(f"git clone failed: Cloning into '{folder}'... not found")


@pytest.fixture
def quick(analyzer_with):
    """The analyzer with limits short enough for a test that waits for a second attempt."""

    def analyzer(**parts):
        return analyzer_with(policy=QUICK, **parts)

    return analyzer


@pytest.fixture
def worker_serving(temporal, task_queue):
    """A worker built the way the process builds it, on the test's own queue."""

    def worker(analyzer) -> Worker:
        agent = replace(analyzer.registration, task_queue=task_queue)
        return main.build_worker(temporal, agent, max_concurrent_activities=2)

    return worker


@pytest.fixture
def execute(temporal, task_queue, worker_serving):
    """Run the workflow of a run on such a worker."""

    async def execute(run: AnalyzeRepositoryInput, analyzer) -> str:
        """Returns everything the workflow history holds, as text."""
        async with worker_serving(analyzer):
            handle = await temporal.start_workflow(
                ANALYZE_REPOSITORY_WORKFLOW,
                run,
                id=analysis_workflow_id(run.run_id),
                task_queue=task_queue,
            )
            try:
                await asyncio.wait_for(handle.result(), timeout=60)
            except WorkflowFailureError:
                pass
            return readable(await handle.fetch_history())

    return execute


# -------------------------------------------------------------- how a run ends
async def test_run_ends_succeeded_with_the_one_profile_version_it_stored(
    execute, quick, run, database
):
    assert database.run(run).status is AnalysisRunStatus.QUEUED
    await execute(run, quick())
    stored = database.run(run)
    assert (stored.status, stored.profile_version, stored.error) == (
        AnalysisRunStatus.SUCCEEDED,
        1,
        None,
    )
    assert stored.started_at is not None and stored.finished_at is not None
    assert database.profile_versions(run) == 1


async def test_refused_repository_ends_the_run_failed_with_a_reason_fit_to_show(
    execute, quick, intake, model, run, database
):
    intake.failures.append(not_found_in)
    await execute(run, quick())
    stored = database.run(run)
    assert (stored.status, stored.error) == (
        AnalysisRunStatus.FAILED,
        "git clone failed: Cloning into '<clone>/repo'... not found",
    )
    assert len(intake.cloned) == 1
    assert model.requests == []
    assert database.profile_versions(run) == 0


async def test_model_provider_failure_is_tried_again_and_the_run_succeeds(
    execute, quick, intake, model, run, database
):
    model.turns.append(RATE_LIMITED)
    await execute(run, quick())
    assert database.run(run).status is AnalysisRunStatus.SUCCEEDED
    assert database.profile_versions(run) == 1
    # The second attempt asked the model again, on the clone the first one read.
    assert (len(model.requests), len(intake.cloned)) == (2, 1)


async def test_request_the_model_provider_refuses_ends_the_run_at_the_first_attempt(
    execute, quick, model, run, database
):
    model.turns.append(LLMError("the model provider answered HTTP 401", status=401))
    await execute(run, quick())
    stored = database.run(run)
    assert (stored.status, stored.error) == (
        AnalysisRunStatus.FAILED,
        "the model provider answered HTTP 401",
    )
    assert len(model.requests) == 1


async def test_run_without_a_key_of_the_model_provider_ends_failed_at_the_first_attempt(
    execute, quick, intake, run, database
):
    def no_key(workspace_id: uuid.UUID):
        raise ModelKeyNotSet("OPENROUTER_API_KEY")

    await execute(run, quick(new_model=no_key))
    stored = database.run(run)
    assert (stored.status, stored.error) == (
        AnalysisRunStatus.FAILED,
        "the model provider's API key is not set: set it in the dashboard's Settings,"
        " or with the environment variable OPENROUTER_API_KEY",
    )
    assert intake.cloned == []
    assert not database.has_checkpoints(run)


async def test_history_of_a_run_holds_no_credential_of_its_workspace(
    execute, quick, model, sessions, cipher, run, database
):
    database.store_credential(run.workspace_id, OPENROUTER_API_KEY, "sk-stored-0123456789", cipher)
    database.store_credential(run.workspace_id, GITHUB_TOKEN, "ghp_stored0123456789", cipher)
    credentials = WorkspaceCredentials(sessions, {}, cipher)
    router = ModelRouter.from_file(MODELS, env={})
    used: list[str] = []

    def new_model(workspace_id: uuid.UUID):
        used.append(credentials.model_key(workspace_id, router))
        return model

    tokens = WorkspaceRepositoryTokens(credentials, {})
    history = await execute(run, quick(new_model=new_model, tokens_of=tokens.of))
    assert database.run(run).status is AnalysisRunStatus.SUCCEEDED
    assert set(used) == {"sk-stored-0123456789"}
    assert "sk-stored-0123456789" not in history and "ghp_stored0123456789" not in history


async def test_failure_that_keeps_happening_ends_the_run_once_attempts_are_used_up(
    execute, quick, model, run, database
):
    model.turns.extend([RATE_LIMITED] * QUICK.max_attempts)
    await execute(run, quick())
    stored = database.run(run)
    assert (stored.status, stored.error) == (
        AnalysisRunStatus.FAILED,
        "the model provider answered HTTP 429",
    )
    assert len(model.requests) == QUICK.max_attempts


async def test_unexpected_error_ends_the_run_failed_without_its_detail(
    execute, quick, model, run, database
):
    model.turns.extend([RuntimeError("/Users/someone/secret")] * QUICK.max_attempts)
    await execute(run, quick())
    stored = database.run(run)
    assert (stored.status, stored.error) == (
        AnalysisRunStatus.FAILED,
        "the analysis stopped unexpectedly",
    )


async def test_attempt_that_takes_too_long_ends_the_run_as_timed_out(
    execute, analyzer_with, model, run, database
):
    release = threading.Event()
    model.meanwhile = lambda: release.wait(timeout=30)
    impatient = AnalysisPolicy(attempt_timeout_seconds=2, max_attempts=1)
    try:
        await execute(run, analyzer_with(policy=impatient))
    finally:
        release.set()
    stored = database.run(run)
    assert (stored.status, stored.error) == (
        AnalysisRunStatus.FAILED,
        "the analysis did not finish in time",
    )


async def test_cancelled_run_ends_failed_and_leaves_no_checkpoints_and_no_clone(
    temporal, task_queue, worker_serving, analyzer_with, model, run, database
):
    asked, release = threading.Event(), threading.Event()

    def held() -> None:
        asked.set()
        release.wait(timeout=30)

    model.meanwhile = held
    # A cancellation reaches an activity in the answer to a heartbeat: three in three seconds.
    analyzer = analyzer_with(policy=AnalysisPolicy(heartbeat_timeout_seconds=3, max_attempts=1))
    async with worker_serving(analyzer):
        handle = await temporal.start_workflow(
            ANALYZE_REPOSITORY_WORKFLOW,
            run,
            id=analysis_workflow_id(run.run_id),
            task_queue=task_queue,
        )
        try:
            assert await asyncio.to_thread(asked.wait, 30)
            await handle.cancel()
            with pytest.raises(WorkflowFailureError) as stopped:
                await asyncio.wait_for(handle.result(), timeout=60)
        finally:
            release.set()
        # The attempt that was given up on is still in its thread: it finds its run failed.
        await asyncio.wait_for(database.checkpoints_gone(run), timeout=20)
    assert isinstance(stopped.value.__cause__, CancelledError)
    stored = database.run(run)
    assert (stored.status, stored.error) == (
        AnalysisRunStatus.FAILED,
        "the analysis was cancelled",
    )
    assert database.profile_versions(run) == 0
    assert not analyzer.clones.folder_of(run.run_id).exists()


async def test_run_of_a_project_in_another_workspace_is_not_executed(
    execute, quick, intake, run, database
):
    await execute(replace(run, workspace_id=database.new_workspace()), quick())
    assert intake.cloned == []
    assert database.run(run).status is AnalysisRunStatus.QUEUED
    assert database.profile_versions(run) == 0


# ------------------------------------------------ what a run leaves behind
async def test_run_that_succeeded_after_a_failed_attempt_leaves_no_checkpoints(
    execute, quick, model, run, database
):
    model.turns.append(RATE_LIMITED)
    await execute(run, quick())
    assert database.run(run).status is AnalysisRunStatus.SUCCEEDED
    assert not database.has_checkpoints(run)


async def test_run_that_used_up_its_attempts_leaves_no_checkpoints(
    execute, quick, model, run, database
):
    outage = LLMError("the model provider answered HTTP 503", status=503)
    model.turns.extend([outage] * QUICK.max_attempts)
    await execute(run, quick())
    assert database.run(run).status is AnalysisRunStatus.FAILED
    assert not database.has_checkpoints(run)


async def test_refused_analysis_leaves_no_checkpoints(execute, quick, model, run, database):
    model.turns.append(AnalysisError("no profile after 40 steps"))
    await execute(run, quick())
    assert database.run(run).status is AnalysisRunStatus.FAILED
    assert not database.has_checkpoints(run)


async def test_run_that_succeeded_leaves_no_clone_on_the_disk(
    execute, quick, clones_root, intake, run
):
    await execute(run, quick())
    assert len(intake.cloned) == 1
    assert list(clones_root.iterdir()) == []


async def test_run_that_failed_leaves_no_clone_on_the_disk(
    execute, quick, clones_root, intake, model, run
):
    model.turns.append(AnalysisError("no profile after 40 steps"))
    await execute(run, quick())
    assert len(intake.cloned) == 1
    assert list(clones_root.iterdir()) == []


async def test_retried_attempt_continues_the_analysis_of_the_one_before(
    execute, quick, intake_of_a_local_repository, model, turns, run, database
):
    model.turns.extend([turns.reads_the_readme, RATE_LIMITED, turns.submits])
    await execute(run, quick(intake=intake_of_a_local_repository))
    assert database.run(run).status is AnalysisRunStatus.SUCCEEDED
    assert len(model.requests) == 3
    assert not database.has_checkpoints(run)


# ------------------------------------------------------ what the history holds
async def test_history_of_a_continued_run_holds_nothing_the_analyzer_read(
    execute, quick, intake_of_a_local_repository, model, turns, run
):
    model.turns.extend([turns.reads_the_readme, RATE_LIMITED, turns.submits])
    history = await execute(run, quick(intake=intake_of_a_local_repository))
    assert str(run.run_id) in history
    assert "Share memories offline." not in history
    assert PROFILE_NAME not in history


async def test_history_of_a_succeeded_run_holds_its_identifiers_and_no_profile(execute, quick, run):
    history = await execute(run, quick())
    assert str(run.run_id) in history
    assert PROFILE_NAME not in history
    assert "README" not in history


async def test_history_of_a_succeeded_run_names_no_repository_and_no_folder_of_the_worker(
    execute, quick, clones_root, source_repo, intake_of_a_local_repository, run
):
    history = await execute(run, quick(intake=intake_of_a_local_repository))
    assert "example.com" not in history
    for folder in (clones_root, source_repo):
        assert str(folder) not in history
        assert str(folder.resolve()) not in history


async def test_history_holds_of_the_clone_the_run_it_was_asked_for_and_the_commit_it_read(
    temporal, execute, quick, run, database
):
    await execute(run, quick())
    history = await temporal.get_workflow_handle(analysis_workflow_id(run.run_id)).fetch_history()
    # What each activity was given and what it answered with, in the order of the history.
    exchanged = [
        json.loads(payload.data)
        for event in history.events
        for payloads in (
            event.activity_task_scheduled_event_attributes.input.payloads,
            event.activity_task_completed_event_attributes.result.payloads,
        )
        for payload in payloads
        if payload.data
    ]
    asked = next(entry for entry in exchanged if entry.get("name") == "clone_repository")
    answered = next(entry for entry in exchanged if "succeeded" in entry)
    assert asked["action_id"].startswith(f"{run_thread_id(run.run_id)}:")
    assert asked["payload"] == {name: str(value) for name, value in vars(run).items()}
    assert answered == {
        "action_id": asked["action_id"],
        "succeeded": True,
        "result": {"commit_sha": "a" * 40},
        "error": "",
    }
    finished = next(entry for entry in exchanged if entry.get("output") is not None)
    assert finished == {
        "output": {"profile_id": str(database.run(run).profile_id)},
        "action": None,
    }


async def test_history_of_a_failed_run_does_not_name_the_clone_folder(
    execute, quick, clones_root, intake, run
):
    intake.failures.append(not_found_in)
    history = await execute(run, quick())
    assert "Cloning into '<clone>/repo'" in history
    assert str(clones_root) not in history
    assert str(clones_root.resolve()) not in history


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
    worker_serving, sessions, checkpoints, analyzer, leave_a_checkpoint, some_run, database
):
    never_recorded = some_run()
    with checkpoints.of_attempt(run_thread_id(never_recorded.run_id)) as left_behind:
        leave_a_checkpoint(left_behind)
    stop = asyncio.Event()
    serving = asyncio.create_task(
        main.serve_until(
            stop, worker_serving(analyzer), sessions, [wiring.cleanup_of_analysis_runs(QUICK)]
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


async def test_worker_does_not_start_with_an_encryption_key_that_is_not_one():
    environ = {**DATABASE, **MODEL, "ENCRYPTION_KEY": "made-up-and-not-a-key"}
    with pytest.raises(main.StartupError, match="ENCRYPTION_KEY is not base64") as refused:
        await main.serve(environ)
    assert "made-up-and-not-a-key" not in str(refused.value)


async def test_worker_without_the_key_of_the_model_provider_gets_as_far_as_temporal():
    environ = {**DATABASE, "LLM_CONFIG_PATH": MODELS, "TEMPORAL_ADDRESS": "127.0.0.1:1"}
    with pytest.raises(main.StartupError, match="TEMPORAL_ADDRESS"):
        await main.serve(environ)


async def test_worker_does_not_start_without_its_model_configuration(tmp_path):
    with pytest.raises(main.StartupError):
        await main.serve({**DATABASE, "LLM_CONFIG_PATH": str(tmp_path / "missing.yaml")})


async def test_unreachable_temporal_is_reported_with_the_setting_to_change():
    environ = {**DATABASE, **MODEL, "TEMPORAL_ADDRESS": "127.0.0.1:1"}
    with pytest.raises(main.StartupError, match=r"127\.0\.0\.1:1 \(TEMPORAL_ADDRESS\)"):
        await main.serve(environ)
