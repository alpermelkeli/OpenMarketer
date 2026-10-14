"""Tests for how the repository analyzer is set up from the environment when the worker starts.

They need no Temporal server and no model. A database is named, never connected to, and no
credential is resolved: how one is, is tested with ``WorkspaceCredentials`` and with the
analyzer's tokens. The tests that make the chat model of an attempt read the workspace's
model choices, so they need PostgreSQL (see the root ``conftest.py``). The
worker's own folder of clones is under the system's temporary folder, which every test of the
worker has replaced with one of its own (``temporary_folder`` in ``conftest.py``).
"""

import os
import time
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.session import DatabaseError, session_factory
from openmarketer_core.llm import RouterChatModel
from openmarketer_core.repository_analysis.intake import (
    NO_TOKENS,
    IntakeError,
    RepositoryTokens,
)
from openmarketer_core.repository_analysis.workflow_contract import ANALYSIS_TASK_QUEUE
from openmarketer_worker.agent_runtime.activity_support import checkpoints_in
from openmarketer_worker.agent_runtime.vocabulary import FORGET_RUN, RUN_ACTION, RUN_GRAPH_STEP
from openmarketer_worker.credentials import WorkspaceCredentials
from openmarketer_worker.repository_analyzer import steps, wiring
from openmarketer_worker.repository_analyzer.policy import AnalysisPolicy, longest_analysis_seconds
from openmarketer_worker.repository_analyzer.workflow import AnalyzeRepository
from openmarketer_worker.settings import SettingsError

MODELS = str(Path(__file__).resolve().parents[4] / "config" / "models.yaml")
MODEL = {"LLM_CONFIG_PATH": MODELS, "OPENROUTER_API_KEY": "not-a-real-key"}
DATABASE_URL = "postgresql+psycopg://app:hunter2@db.internal:5432/marketer"


def wired(
    environ: dict[str, str],
    database_url: str = DATABASE_URL,
    sessions: sessionmaker[Session] | None = None,
):
    sessions = sessions or session_factory(DATABASE_URL)
    credentials = WorkspaceCredentials(sessions, environ, None)
    return wiring.repository_analyzer(environ, sessions, database_url, credentials)


@pytest.fixture
def parts(monkeypatch) -> list[wiring.AnalyzerParts]:
    """What the wiring built the analyzer of, each time it was asked."""
    built: list[wiring.AnalyzerParts] = []
    register = wiring.registration

    def registration(parts: wiring.AnalyzerParts):
        built.append(parts)
        return register(parts)

    monkeypatch.setattr(wiring, "registration", registration)
    return built


def test_tokens_of_the_run_reach_the_clone_as_they_were_resolved(monkeypatch, tmp_path):
    offered: list[RepositoryTokens] = []

    def run_intake(source, clone_into, *, tokens, commit):
        offered.append(tokens)

    monkeypatch.setattr(wiring, "run_intake", run_intake)
    tokens = RepositoryTokens.from_environment({"GITHUB_TOKEN": "github-token-value"})
    wiring.intake("https://example.com/acme/app.git", tmp_path / "repo", None, tokens)
    assert offered == [tokens]


def test_commit_of_the_run_reaches_the_clone(monkeypatch, tmp_path):
    asked: list[str | None] = []
    monkeypatch.setattr(
        wiring, "run_intake", lambda source, clone_into, *, tokens, commit: asked.append(commit)
    )
    wiring.intake("https://example.com/acme/app.git", tmp_path / "repo", "a" * 40, NO_TOKENS)
    assert asked == ["a" * 40]


def test_each_attempt_gets_a_chat_model_of_its_own_from_one_configuration(parts, sessions):
    wired(MODEL, sessions=sessions)
    workspace_id = uuid.uuid4()
    first, second = parts[0].new_model(workspace_id), parts[0].new_model(workspace_id)
    assert isinstance(first, RouterChatModel) and isinstance(second, RouterChatModel)
    assert first is not second
    assert first.router.config is second.router.config


def test_chat_model_of_an_attempt_carries_the_key_of_the_environment(parts, sessions):
    wired(MODEL, sessions=sessions)
    model = parts[0].new_model(uuid.uuid4())
    assert isinstance(model, RouterChatModel)
    assert model.api_key == MODEL["OPENROUTER_API_KEY"]


def test_no_credential_is_in_the_text_of_what_the_analyzer_is_built_of(parts, sessions):
    secrets = {
        "OPENROUTER_API_KEY": "made-up-model-key",
        "GITHUB_TOKEN": "made-up-github-token",
        "GITLAB_TOKEN": "made-up-gitlab-token",
    }
    wired({"LLM_CONFIG_PATH": MODELS, **secrets}, sessions=sessions)
    workspace_id = uuid.uuid4()
    shown = [
        repr(parts[0]),
        str(parts[0]),
        repr(parts[0].new_model(workspace_id)),
        repr(parts[0].tokens_of(workspace_id)),
    ]
    assert all(secret not in text for secret in secrets.values() for text in shown)


def test_database_password_is_not_in_the_text_of_what_reaches_the_checkpoints():
    checkpoints = checkpoints_in("postgresql+psycopg://app:hunter2@db.internal:5432/marketer")
    shown = [
        repr(checkpoints),
        str(checkpoints),
        repr(checkpoints.of_attempt),
        repr(checkpoints.forget),
    ]
    assert all("hunter2" not in text and "db.internal" not in text for text in shown)


# ------------------------------------------------------------- the clones
def test_clones_are_kept_in_a_folder_of_the_worker_under_the_temporary_folder(
    parts, temporary_folder
):
    wired(MODEL)
    run_id = uuid.uuid4()
    assert parts[0].clones.folder_of(run_id) == (
        temporary_folder / "openmarketer-analysis" / str(run_id)
    )


def test_clones_older_than_a_run_can_last_are_swept_when_the_worker_starts(temporary_folder):
    limits = {"ANALYSIS_TIMEOUT_MINUTES": "10", "ANALYSIS_MAX_ATTEMPTS": "2"}
    longest = longest_analysis_seconds(AnalysisPolicy(attempt_timeout_seconds=600, max_attempts=2))
    root = temporary_folder / "openmarketer-analysis"
    left_behind, in_progress = root / "left-behind", root / "in-progress"
    for folder, age in ((left_behind, longest + 60), (in_progress, longest - 60)):
        folder.mkdir(parents=True)
        os.utime(folder, (time.time() - age, time.time() - age))
    wired({**MODEL, **limits})
    assert (left_behind.exists(), in_progress.exists()) == (False, True)


def test_folder_of_clones_that_cannot_be_the_workers_is_an_error_of_the_system(temporary_folder):
    (temporary_folder / "openmarketer-analysis").write_text("not a folder")
    with pytest.raises(OSError):
        wired(MODEL)


# ------------------------------------------------- what the worker is handed
def test_worker_is_handed_the_workflow_and_its_activities_on_the_analysis_queue():
    agent = wired(MODEL)
    assert agent.task_queue == ANALYSIS_TASK_QUEUE
    assert list(agent.workflows) == [AnalyzeRepository]
    assert [getattr(activity, "__name__", "") for activity in agent.activities] == [
        RUN_GRAPH_STEP,
        RUN_ACTION,
        FORGET_RUN,
        steps.START_RUN,
        steps.RECORD_FAILURE,
    ]


def test_cleanup_is_the_rule_of_analysis_runs_with_the_longest_run_of_the_configured_limits(
    monkeypatch,
):
    asked: list[tuple[object, timedelta]] = []

    def rule(session: object, *, longest_run: timedelta) -> int:
        asked.append((session, longest_run))
        return 3

    monkeypatch.setattr(wiring, "remove_checkpoints_of_analysis_runs", rule)
    agent = wired({**MODEL, "ANALYSIS_TIMEOUT_MINUTES": "10", "ANALYSIS_MAX_ATTEMPTS": "2"})
    (cleanup,) = agent.checkpoint_cleanups
    session = Session()

    assert (cleanup.runs, cleanup.remove(session)) == ("analysis runs", 3)
    configured = AnalysisPolicy(attempt_timeout_seconds=600, max_attempts=2)
    assert asked == [(session, timedelta(seconds=longest_analysis_seconds(configured)))]


def test_limit_that_is_not_a_number_is_a_settings_error_naming_it():
    with pytest.raises(SettingsError, match="ANALYSIS_MAX_ATTEMPTS"):
        wired({**MODEL, "ANALYSIS_MAX_ATTEMPTS": "soon"})


def test_analyzer_is_built_without_a_key_of_the_model_provider(parts):
    wired({"LLM_CONFIG_PATH": MODELS})
    assert len(parts) == 1


def test_token_host_that_is_not_a_host_is_an_intake_error():
    with pytest.raises(IntakeError, match="GITLAB_HOST"):
        wired({**MODEL, "GITLAB_HOST": "https://gitlab.example.com"})


def test_database_that_cannot_keep_checkpoints_is_a_database_error():
    with pytest.raises(DatabaseError, match="checkpoints need a PostgreSQL database"):
        wired(MODEL, database_url="sqlite://")
