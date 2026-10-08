"""Tests for the worker as it is assembled: the workflow with the real activities.

A run goes through Temporal (the dev stack's server, see ``conftest.py``) and
PostgreSQL; only cloning and the model are scripted. The wiring from the
environment is tested without either.
"""

import base64
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest
from google.protobuf.json_format import MessageToDict
from temporalio.client import WorkflowFailureError, WorkflowHistory

from openmarketer_core.analysis_workflow import (
    ANALYZE_REPOSITORY_WORKFLOW,
    AnalyzeRepositoryInput,
    analysis_workflow_id,
)
from openmarketer_core.db.models import AnalysisRunStatus
from openmarketer_core.intake import IntakeError
from openmarketer_core.llm import LLMError, RouterChatModel
from openmarketer_core.repository_analysis import RepositoryAnalysis
from openmarketer_worker import main
from openmarketer_worker.activities import AnalysisActivities
from openmarketer_worker.policy import AnalysisPolicy

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
def execute(temporal, task_queue, sessions):
    """Run the workflow of a run on a worker built the way the process builds it."""

    async def execute(run: AnalyzeRepositoryInput, analyse) -> str:
        """Returns everything the workflow history holds, as text."""
        worker = main.build_worker(
            temporal,
            AnalysisActivities(sessions, analyse, QUICK),
            task_queue=task_queue,
            max_concurrent_activities=2,
        )
        async with worker:
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

    def analyse(repository_url: str, clone_into: Path) -> RepositoryAnalysis:
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


async def test_history_of_a_succeeded_run_holds_its_identifiers_and_no_profile(
    execute, analysis, run
):
    history = await execute(run, analysis)
    assert str(run.run_id) in history
    assert PROFILE_NAME not in history
    assert "example.com" not in history


async def test_history_of_a_failed_run_does_not_name_the_clone_folder(execute, run):
    clones: list[Path] = []

    def analyse(repository_url: str, clone_into: Path) -> RepositoryAnalysis:
        clones.append(clone_into)
        raise IntakeError(f"git clone failed: Cloning into '{clone_into}'... not found")

    history = await execute(run, analyse)
    assert "Cloning into '<clone>/repo'" in history
    assert str(clones[0].parent) not in history
    assert str(clones[0].parent.resolve()) not in history


# ------------------------------------------------------- wiring from the environment
@pytest.fixture
def analysed_with(monkeypatch) -> list[dict]:
    """Replace the analysis itself and collect what the worker would have run it with."""
    calls: list[dict] = []

    def analyze_repository(source: str, clone_into: Path, **dependencies) -> None:
        calls.append({"source": source, **dependencies})

    monkeypatch.setattr(main, "analyze_repository", analyze_repository)
    return calls


def test_tokens_of_the_environment_reach_the_analysis_bound_to_their_hosts(analysed_with, tmp_path):
    analyse = main.analysis_with_configured_models({**MODEL, "GITHUB_TOKEN": "github-token-value"})
    analyse("https://example.com/acme/app.git", tmp_path)
    tokens = analysed_with[0]["tokens"]
    assert tokens.token_for("https://github.com/acme/app.git") == "github-token-value"
    assert tokens.token_for("https://example.com/acme/app.git") is None


def test_each_run_gets_a_chat_model_of_its_own(analysed_with, tmp_path):
    analyse = main.analysis_with_configured_models(MODEL)
    analyse("https://example.com/acme/app.git", tmp_path)
    analyse("https://example.com/acme/app.git", tmp_path)
    first, second = (call["model"] for call in analysed_with)
    assert isinstance(first, RouterChatModel)
    assert first is not second
    assert first.router is second.router


async def test_worker_does_not_start_without_a_database_url():
    with pytest.raises(main.StartupError, match="DATABASE_URL"):
        await main.serve(MODEL)


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
