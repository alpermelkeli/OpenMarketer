"""Fixtures for the worker tests: a committed run to execute and a Temporal server.

Activities are tested against PostgreSQL (see the root ``conftest.py``); a run
commits its own transactions, so rows are committed here too. Workflows are
tested against the dev stack's Temporal server (``make up``) on a task queue
of their own, and are skipped when no server is reachable, the way database
tests are. The analysis itself is always replaced: no clone, no model.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from temporalio.client import Client

from openmarketer_core.analysis_workflow import AnalyzeRepositoryInput
from openmarketer_core.analyzer import Analysis
from openmarketer_core.db.analysis_runs import AnalysisRun, analysis_run, request_analysis_run
from openmarketer_core.db.models import ProductProfileRecord, Project, Workspace
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.extraction import ExtractionResult
from openmarketer_core.intake import IntakeResult, RepoFiles, Snapshot
from openmarketer_core.profile import ProductProfile
from openmarketer_core.repository_analysis import RepositoryAnalysis

DEV_STACK_TEMPORAL = "localhost:7233"
PROFILE = ProductProfile.model_validate({"product": {"name": "Example App", "type": "dev_tool"}})


@dataclass
class ScriptedAnalysis:
    """Stands in for cloning and analysing: returns a fixed draft, or raises what it is given."""

    failures: list[Exception] = field(default_factory=list)
    ref: str = "main"
    meanwhile: Callable[[], None] | None = None  # what happens elsewhere while it runs

    def __post_init__(self) -> None:
        self.cloned: list[tuple[str, Path]] = []

    def __call__(self, repository_url: str, clone_into: Path) -> RepositoryAnalysis:
        self.cloned.append((repository_url, clone_into))
        clone_into.mkdir()
        if self.meanwhile is not None:
            self.meanwhile()
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


@pytest.fixture
def analysis() -> ScriptedAnalysis:
    """An analysis that succeeds."""
    return ScriptedAnalysis()


@pytest.fixture
def scripted() -> type[ScriptedAnalysis]:
    """For tests that script their own analysis."""
    return ScriptedAnalysis


@pytest.fixture
def sessions(engine) -> sessionmaker[Session]:
    return session_factory(engine.url.render_as_string(hide_password=False))


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
