"""Fixtures for the worker tests: a committed run to execute and a Temporal server.

Activities are tested against PostgreSQL (see the root ``conftest.py``); a run
commits its own transactions, so rows are committed here too. Workflows are
tested against the dev stack's Temporal server (``make up``) on a task queue
of their own, and are skipped when no server is reachable, the way database
tests are. The analysis itself is replaced here: no clone, no model
(``repository_analyzer/test_checkpointed_analysis.py`` runs the real one on a local
repository).
The checkpoints of a run are the real ones, in PostgreSQL.
"""

import asyncio
import json
import os
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker
from temporalio.client import Client

from openmarketer_core.db.analysis_runs import (
    AnalysisRun,
    analysis_run,
    request_analysis_run,
    run_thread_id,
)
from openmarketer_core.db.checkpoint_schema import CHECKPOINT_TABLES_OF_THREADS
from openmarketer_core.db.models import ProductProfileRecord, Project, Workspace
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.llm import ChatReply
from openmarketer_core.profile import ProductProfile
from openmarketer_core.repository_analysis.analyzer_agent import Analysis
from openmarketer_core.repository_analysis.extraction import ExtractionResult
from openmarketer_core.repository_analysis.intake import IntakeResult, RepoFiles, Snapshot
from openmarketer_core.repository_analysis.pipeline import RepositoryAnalysis, analyze_repository
from openmarketer_core.repository_analysis.workflow_contract import AnalyzeRepositoryInput
from openmarketer_worker.repository_analyzer import wiring
from openmarketer_worker.repository_analyzer.activities import CheckpointAccess

DEV_STACK_TEMPORAL = "localhost:7233"
PROFILE = ProductProfile.model_validate({"product": {"name": "Example App", "type": "dev_tool"}})


@dataclass
class ScriptedAnalysis:
    """Stands in for cloning and analysing: returns a fixed draft, or raises what it is given.

    Either way it stores a checkpoint before and after what happens meanwhile, as the
    steps of a real attempt do.
    """

    failures: list[Exception] = field(default_factory=list)
    ref: str = "main"
    meanwhile: Callable[[], None] | None = None  # what happens elsewhere while it runs

    def __post_init__(self) -> None:
        self.cloned: list[tuple[str, Path]] = []

    def __call__(
        self, repository_url: str, clone_into: Path, checkpoints: RunCheckpoints
    ) -> RepositoryAnalysis:
        self.cloned.append((repository_url, clone_into))
        clone_into.mkdir()
        store_one_checkpoint(checkpoints)
        if self.meanwhile is not None:
            self.meanwhile()
        store_one_checkpoint(checkpoints)
        if self.failures:
            raise self.failures.pop(0)
        snapshot = Snapshot(
            root=clone_into, source_url=repository_url, commit_sha="a" * 40, ref=self.ref
        )
        return RepositoryAnalysis(
            intake=IntakeResult(snapshot=snapshot, files=RepoFiles(clone_into), findings=[]),
            extraction=ExtractionResult(),
            analysis=Analysis(profile=PROFILE, cost_usd=0.0, steps=1, model="scripted"),
        )


def store_one_checkpoint(checkpoints: RunCheckpoints) -> None:
    checkpoints.store.put(
        {"configurable": {"thread_id": checkpoints.thread_id, "checkpoint_ns": ""}},
        empty_checkpoint(),
        {"source": "loop", "step": 1},
        {},
    )


@pytest.fixture
def analysis() -> ScriptedAnalysis:
    """An analysis that succeeds."""
    return ScriptedAnalysis()


@pytest.fixture
def scripted() -> type[ScriptedAnalysis]:
    """For tests that script their own analysis."""
    return ScriptedAnalysis


README = "# Example App\n\nShare memories offline.\n"
EVIDENCED_PROFILE = {
    "product": {
        "name": "Example App",
        "type": "dev_tool",
        "evidence": [{"file": "README.md", "lines": "1-1"}],
        "confidence": 0.9,
    }
}


def tool_call(name: str, **arguments: object) -> list[dict]:
    """An assistant turn that calls one tool."""
    function = {"name": name, "arguments": json.dumps(arguments)}
    return [{"id": f"call-{name}", "type": "function", "function": function}]


READS_THE_README = tool_call("read_file", path="README.md")
SUBMITS_THE_PROFILE = tool_call("submit_profile", **EVIDENCED_PROFILE)


@dataclass
class AnalysisOfALocalRepository:
    """The real analysis with the run's checkpoints, on a local repository with a scripted model.

    Two things are replaced, both at the edge. The clone: the worker clones only
    ``https://`` URLs, so the URL of the project is set aside and ``source`` is
    cloned, with real git. The model: it replays ``turns`` (the tool calls of an
    assistant turn, or an exception to raise) across attempts and records what
    it was sent. Intake, the secret scan, the analyzer graph and the checkpoint
    store are the real ones.
    """

    source: Path
    turns: list[list[dict] | Exception]

    def __post_init__(self) -> None:
        self.requests: list[list[dict]] = []  # the conversation sent with each model call
        self.results: list[RepositoryAnalysis] = []

    def __call__(
        self, repository_url: str, clone_into: Path, checkpoints: RunCheckpoints
    ) -> RepositoryAnalysis:
        result = analyze_repository(
            str(self.source), clone_into, model=self, extractors=[], checkpoints=checkpoints
        )
        self.results.append(result)
        return result

    def chat(self, role, messages, *, tools=None, tool_choice=None) -> ChatReply:
        self.requests.append([dict(message) for message in messages])
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        message = {"role": "assistant", "content": None, "tool_calls": turn}
        return ChatReply(message=message, model="fake/model", cost_usd=0.01)


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    """A git repository on this machine with one commit."""
    if shutil.which("gitleaks") is None:
        pytest.skip("needs gitleaks")
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "README.md").write_text(README)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "first"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)
    return repo


@pytest.fixture
def analysis_of_a_local_repository(source_repo):
    """The real analysis of ``source_repo`` with a model that replays the given turns."""

    def analysis(*turns: list[dict] | Exception) -> AnalysisOfALocalRepository:
        return AnalysisOfALocalRepository(source_repo, list(turns))

    return analysis


@pytest.fixture
def turns() -> SimpleNamespace:
    """The assistant turns the tests script."""
    return SimpleNamespace(reads_the_readme=READS_THE_README, submits=SUBMITS_THE_PROFILE)


@pytest.fixture
def sessions(engine) -> sessionmaker[Session]:
    return session_factory(engine.url.render_as_string(hide_password=False))


@pytest.fixture
def checkpoints(engine) -> CheckpointAccess:
    """The checkpoints of runs in the test database, reached the way the worker reaches them."""
    return wiring.checkpoints_in(engine.url.render_as_string(hide_password=False))


@dataclass(frozen=True)
class Database:
    """What a test reads back from the database, and the rows it sets up."""

    sessions: sessionmaker[Session]

    def new_workspace(self) -> uuid.UUID:
        with transaction(self.sessions) as session:
            workspace = Workspace(name="Acme")
            session.add(workspace)
            session.flush()
            return workspace.id

    def requested_run(
        self, repository_url: str = "https://example.com/acme/app.git"
    ) -> AnalyzeRepositoryInput:
        """A queued run of a new project in a new workspace."""
        workspace_id = self.new_workspace()
        with transaction(self.sessions) as session:
            project = Project(
                workspace_id=workspace_id, name="Example App", source_repo_url=repository_url
            )
            session.add(project)
            session.flush()
            run = request_analysis_run(session, workspace_id=workspace_id, project_id=project.id)
            return AnalyzeRepositoryInput(
                workspace_id=workspace_id, project_id=project.id, run_id=run.id
            )

    def run(self, run: AnalyzeRepositoryInput) -> AnalysisRun:
        with transaction(self.sessions) as session:
            return analysis_run(
                session,
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                run_id=run.run_id,
            )

    def profile_versions(self, run: AnalyzeRepositoryInput) -> int:
        with transaction(self.sessions) as session:
            return session.execute(
                select(func.count())
                .select_from(ProductProfileRecord)
                .where(ProductProfileRecord.project_id == run.project_id)
            ).scalar_one()

    def has_checkpoints(self, run: AnalyzeRepositoryInput) -> bool:
        """Whether any checkpoint table holds a row of the run's thread."""
        thread = {"thread": run_thread_id(run.run_id)}
        with transaction(self.sessions) as session:
            return any(
                session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE thread_id = :thread"), thread
                ).scalar_one()
                for table in CHECKPOINT_TABLES_OF_THREADS
            )

    async def checkpoints_gone(self, run: AnalyzeRepositoryInput) -> None:
        """Returns once the run has no checkpoints; the caller limits the wait."""
        while self.has_checkpoints(run):
            await asyncio.sleep(0.05)


@pytest.fixture
def database(sessions) -> Database:
    return Database(sessions)


@pytest.fixture
def run(database) -> AnalyzeRepositoryInput:
    return database.requested_run()


@pytest.fixture
async def temporal() -> AsyncIterator[Client]:
    address = os.environ.get("TEMPORAL_ADDRESS", DEV_STACK_TEMPORAL)
    try:
        client = await asyncio.wait_for(Client.connect(address), timeout=5)
    except (RuntimeError, TimeoutError):
        pytest.skip("Temporal is not reachable (run `make up`)")
    yield client


@pytest.fixture
def task_queue() -> str:
    """A queue of the test's own: no other worker, in tests or in development, takes its work."""
    return f"test-{uuid.uuid4()}"


@pytest.fixture
def some_run() -> Callable[[], AnalyzeRepositoryInput]:
    """Identifiers of a run that is in no database, for workflow tests with scripted activities."""

    def make() -> AnalyzeRepositoryInput:
        return AnalyzeRepositoryInput(
            workspace_id=uuid.uuid4(), project_id=uuid.uuid4(), run_id=uuid.uuid4()
        )

    return make
