"""Tests for how the repository analyzer is set up from the environment when the worker starts.

They need no Temporal server and no model: the analysis itself is replaced
where a test looks at what it would be run with. A database is named, never
connected to.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.orm import Session

from openmarketer_core.db.session import DatabaseError, session_factory
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.llm import RouterChatModel
from openmarketer_core.llm_config import ConfigError
from openmarketer_core.repository_analysis.intake import IntakeError
from openmarketer_core.repository_analysis.workflow_contract import ANALYSIS_TASK_QUEUE
from openmarketer_worker.repository_analyzer import steps, wiring
from openmarketer_worker.repository_analyzer.policy import AnalysisPolicy, longest_analysis_seconds
from openmarketer_worker.repository_analyzer.workflow import AnalyzeRepository
from openmarketer_worker.settings import SettingsError

MODELS = str(Path(__file__).resolve().parents[4] / "config" / "models.yaml")
MODEL = {"LLM_CONFIG_PATH": MODELS, "OPENROUTER_API_KEY": "not-a-real-key"}
DATABASE_URL = "postgresql+psycopg://app:hunter2@db.internal:5432/marketer"


def wired(environ: dict[str, str], database_url: str = DATABASE_URL):
    return wiring.repository_analyzer(environ, session_factory(DATABASE_URL), database_url)


@pytest.fixture
def analysed_with(monkeypatch) -> list[dict]:
    """Replace the analysis itself and collect what the worker would have run it with."""
    calls: list[dict] = []

    def analyze_repository(source: str, clone_into: Path, **dependencies) -> None:
        calls.append({"source": source, **dependencies})

    monkeypatch.setattr(wiring, "analyze_repository", analyze_repository)
    return calls


@pytest.fixture
def run_checkpoints() -> RunCheckpoints:
    """Checkpoints of some run; nothing is stored in them here."""
    return RunCheckpoints(store=InMemorySaver(), thread_id="some-run")


def test_tokens_of_the_environment_reach_the_analysis_bound_to_their_hosts(
    analysed_with, run_checkpoints, tmp_path
):
    analyse = wiring.analysis_with_configured_models(
        {**MODEL, "GITHUB_TOKEN": "github-token-value"}
    )
    analyse("https://example.com/acme/app.git", tmp_path, run_checkpoints)
    tokens = analysed_with[0]["tokens"]
    assert tokens.token_for("https://github.com/acme/app.git") == "github-token-value"
    assert tokens.token_for("https://example.com/acme/app.git") is None


def test_checkpoints_of_the_run_reach_the_analysis(analysed_with, run_checkpoints, tmp_path):
    analyse = wiring.analysis_with_configured_models(MODEL)
    analyse("https://example.com/acme/app.git", tmp_path, run_checkpoints)
    assert analysed_with[0]["checkpoints"] is run_checkpoints


def test_each_attempt_gets_a_chat_model_of_its_own(analysed_with, run_checkpoints, tmp_path):
    analyse = wiring.analysis_with_configured_models(MODEL)
    analyse("https://example.com/acme/app.git", tmp_path, run_checkpoints)
    analyse("https://example.com/acme/app.git", tmp_path, run_checkpoints)
    first, second = (call["model"] for call in analysed_with)
    assert isinstance(first, RouterChatModel)
    assert first is not second
    assert first.router is second.router


def test_database_password_is_not_in_the_text_of_what_reaches_the_checkpoints():
    checkpoints = wiring.checkpoints_in(
        "postgresql+psycopg://app:hunter2@db.internal:5432/marketer"
    )
    shown = [
        repr(checkpoints),
        str(checkpoints),
        repr(checkpoints.of_attempt),
        repr(checkpoints.forget),
    ]
    assert all("hunter2" not in text and "db.internal" not in text for text in shown)


# ------------------------------------------------- what the worker is handed
def test_worker_is_handed_the_workflow_and_its_three_activities_on_the_analysis_queue():
    agent = wired(MODEL)
    assert agent.task_queue == ANALYSIS_TASK_QUEUE
    assert list(agent.workflows) == [AnalyzeRepository]
    assert [getattr(activity, "__name__", "") for activity in agent.activities] == [
        steps.START_RUN,
        steps.ANALYSE_AND_STORE,
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


def test_missing_key_of_the_model_provider_is_a_configuration_error():
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        wired({"LLM_CONFIG_PATH": MODELS})


def test_token_host_that_is_not_a_host_is_an_intake_error():
    with pytest.raises(IntakeError, match="GITLAB_HOST"):
        wired({**MODEL, "GITLAB_HOST": "https://gitlab.example.com"})


def test_database_that_cannot_keep_checkpoints_is_a_database_error():
    with pytest.raises(DatabaseError, match="checkpoints need a PostgreSQL database"):
        wired(MODEL, database_url="sqlite://")
