"""Tests for ``openmarketer analyze``. The pipeline stages are replaced by fixed results.

The tests that store a run need PostgreSQL (see the root ``conftest.py``).
"""

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from openmarketer_cli import main
from openmarketer_core.db.models import Evidence, ProductProfileRecord, Project, RepoSnapshot
from openmarketer_core.db.session import DatabaseError
from openmarketer_core.llm import LLMError, RouterChatModel
from openmarketer_core.profile import ProductProfile
from openmarketer_core.repository_analysis.analyzer_agent import Analysis
from openmarketer_core.repository_analysis.extraction import ExtractedFact, ExtractionResult
from openmarketer_core.repository_analysis.intake import (
    NO_TOKENS,
    IntakeResult,
    RepoFiles,
    RepositoryTokens,
    Snapshot,
)

REPOSITORY = "https://example.com/acme/app.git"
PROFILE = ProductProfile.model_validate({"product": {"name": "Example App", "type": "dev_tool"}})
FACT = ExtractedFact(
    extractor="package_json", kind="manifest.name", value="example-app", file="package.json"
)


@pytest.fixture
def offered_tokens() -> list[RepositoryTokens]:
    """The tokens each clone was allowed to choose from."""
    return []


@pytest.fixture
def clones(monkeypatch, offered_tokens) -> list[str]:
    """Replace intake, extraction and the analyzer; return the sources that were cloned."""
    cloned: list[str] = []

    def run_intake(
        source: str, dest: Path, *, tokens: RepositoryTokens = NO_TOKENS
    ) -> IntakeResult:
        cloned.append(source)
        offered_tokens.append(tokens)
        dest.mkdir()
        snapshot = Snapshot(root=dest, source_url=source, commit_sha="a" * 40, ref="main")
        return IntakeResult(snapshot=snapshot, files=RepoFiles(dest), findings=[])

    monkeypatch.setattr(main, "run_intake", run_intake)
    monkeypatch.setattr(main, "discover_extractors", lambda: [])
    monkeypatch.setattr(main, "run_extractors", lambda *_: ExtractionResult(facts=[FACT]))
    monkeypatch.setattr(RouterChatModel, "from_env", lambda: None)
    monkeypatch.setattr(
        main,
        "analyze",
        lambda *_, **__: Analysis(profile=PROFILE, cost_usd=0.0, steps=1, model="scripted"),
    )
    return cloned


@pytest.fixture
def database_url(engine, monkeypatch) -> str:
    url = engine.url.render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)
    return url


def run(*args: str):
    return CliRunner().invoke(main.app, ["analyze", REPOSITORY, *args])


def test_profile_is_printed_and_nothing_is_stored_without_save(clones, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    result = run()
    assert result.exit_code == 0
    assert ProductProfile.model_validate_json(result.stdout) == PROFILE
    assert "stored" not in result.stderr


def test_save_without_database_url_fails_before_anything_is_cloned(clones, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    result = run("--save")
    assert result.exit_code == 1
    assert "DATABASE_URL" in result.stderr
    assert clones == []


def test_save_with_an_unreachable_database_fails_before_anything_is_cloned(clones, monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://nobody@127.0.0.1:1/nothing?connect_timeout=2"
    )
    result = run("--save")
    assert result.exit_code == 1
    assert "make up" in result.stderr
    assert clones == []


def test_save_stores_the_run(clones, database_url, engine):
    result = run("--save")
    assert result.exit_code == 0
    assert "profile version 1, 1 evidence rows" in result.stderr
    with Session(engine) as session:
        project = session.scalars(
            select(Project).where(Project.source_repo_url == REPOSITORY)
        ).one()
        snapshot = session.scalars(
            select(RepoSnapshot).where(RepoSnapshot.project_id == project.id)
        ).one()
        record = session.scalars(
            select(ProductProfileRecord).where(ProductProfileRecord.project_id == project.id)
        ).one()
        evidence = session.scalars(
            select(Evidence).where(Evidence.snapshot_id == snapshot.id)
        ).one()
    assert str(project.id) in result.stderr
    assert snapshot.commit_sha == "a" * 40
    assert record.snapshot_id == snapshot.id
    assert ProductProfile.model_validate(record.content) == PROFILE
    assert evidence.kind == "manifest.name"


def test_profile_is_still_printed_when_saving_fails(clones, database_url, monkeypatch):
    def refuse(*_, **__):
        raise DatabaseError("connection lost")

    monkeypatch.setattr(main, "save_analysis", refuse)
    result = run("--save")
    assert result.exit_code == 1
    assert ProductProfile.model_validate_json(result.stdout) == PROFILE
    assert "the profile was not stored: connection lost" in result.stderr


def test_command_line_does_not_import_sqlalchemy():
    assert "sqlalchemy" not in Path(main.__file__).read_text()


def test_token_of_the_environment_is_offered_to_its_own_host_only(
    clones, offered_tokens, monkeypatch
):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_HOST", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "github-token-value")
    assert run().exit_code == 0
    (tokens,) = offered_tokens
    assert tokens.token_for("https://github.com/acme/app.git") == "github-token-value"
    assert tokens.token_for(REPOSITORY) is None


def test_token_host_that_is_not_a_host_name_stops_the_command(clones, monkeypatch):
    monkeypatch.setenv("GITLAB_HOST", "https://gitlab.example.com")
    result = run()
    assert result.exit_code == 1
    assert "GITLAB_HOST" in result.stderr
    assert clones == []


def test_refusal_by_the_model_provider_is_reported_with_its_status(clones, monkeypatch):
    def refuse(*_, **__):
        raise LLMError("the model provider answered HTTP 401", status=401)

    monkeypatch.setattr(main, "analyze", refuse)
    result = run()
    assert result.exit_code == 1
    assert "error: the model provider answered HTTP 401" in result.stderr
