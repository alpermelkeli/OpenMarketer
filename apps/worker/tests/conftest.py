"""Fixtures for the worker tests: a committed run to execute, what analyses it, and Temporal.

Activities are tested against PostgreSQL (see the root ``conftest.py``); a run
commits its own transactions, so rows are committed here too. Workflows are
tested against the dev stack's Temporal server (``make up``) on a task queue
of their own, and are skipped when no server is reachable, the way database
tests are.

The analyzer is the real one (its graph, its rules, the checkpoints in
PostgreSQL, the clones on a disk under ``tmp_path``) with two things replaced
at the edge, both through ``AnalyzerParts``. The model is ``ScriptedModel``.
The clone is ``ScriptedIntake``, which writes a small repository instead of
reaching a host, or, for the tests that need real git and the secret scan,
``intake_of_a_local_repository``: the worker clones only ``https://`` URLs, so
the URL of the project is set aside and a repository on this machine is cloned.
"""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker
from temporalio.client import Client
from temporalio.testing import ActivityEnvironment

from openmarketer_core.agent_runtime.actions import ActionOutcome, ActionRequest
from openmarketer_core.agent_runtime.steps import GraphRunRef, StepOutcome
from openmarketer_core.db.analysis_runs import (
    AnalysisRun,
    analysis_run,
    request_analysis_run,
    run_thread_id,
)
from openmarketer_core.db.checkpoint_schema import CHECKPOINT_TABLES_OF_THREADS
from openmarketer_core.db.models import ProductProfileRecord, Project, RepoSnapshot, Workspace
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.llm import ChatReply
from openmarketer_core.repository_analysis.intake import (
    IntakeResult,
    RepoFiles,
    Snapshot,
    run_intake,
)
from openmarketer_core.repository_analysis.workflow_contract import (
    ANALYSIS_RUN_KIND,
    AnalyzeRepositoryInput,
)
from openmarketer_worker import AgentRegistration
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess, checkpoints_in
from openmarketer_worker.agent_runtime.vocabulary import (
    FORGET_RUN,
    RUN_ACTION,
    RUN_GRAPH_STEP,
    StepInput,
)
from openmarketer_worker.repository_analyzer import steps, wiring
from openmarketer_worker.repository_analyzer.clones import Intake, LocalClones
from openmarketer_worker.repository_analyzer.policy import AnalysisPolicy

DEV_STACK_TEMPORAL = "localhost:7233"
README = "# Example App\n\nShare memories offline.\n"
EVIDENCED_PROFILE = {
    "product": {
        "name": "Example App",
        "type": "dev_tool",
        "evidence": [{"file": "README.md", "lines": "1-1"}],
        "confidence": 0.9,
    }
}
SCRIPTED_COMMIT = "a" * 40
TEST_POLICY = AnalysisPolicy(attempt_timeout_seconds=600, max_attempts=3)


def tool_call(name: str, **arguments: object) -> list[dict]:
    """An assistant turn that calls one tool."""
    function = {"name": name, "arguments": json.dumps(arguments)}
    return [{"id": f"call-{name}", "type": "function", "function": function}]


READS_THE_README = tool_call("read_file", path="README.md")
SUBMITS_THE_PROFILE = tool_call("submit_profile", **EVIDENCED_PROFILE)


@dataclass
class ScriptedModel:
    """Replays ``turns`` across the attempts of a run and records what it was sent.

    A turn is the tool calls of an assistant turn, or an exception to raise. When the turns
    are used up it submits the profile, so a model that is given none succeeds at once.
    ``meanwhile`` is what happens elsewhere while the model is asked for the first time.
    """

    turns: list[list[dict] | Exception] = field(default_factory=list)
    meanwhile: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        self.requests: list[list[dict]] = []  # the conversation sent with each model call

    def chat(self, role, messages, *, tools=None, tool_choice=None) -> ChatReply:
        self.requests.append([dict(message) for message in messages])
        if self.meanwhile is not None and len(self.requests) == 1:
            self.meanwhile()
        turn = self.turns.pop(0) if self.turns else SUBMITS_THE_PROFILE
        if isinstance(turn, Exception):
            raise turn
        message = {"role": "assistant", "content": None, "tool_calls": turn}
        return ChatReply(message=message, model="fake/model", cost_usd=0.01)


@dataclass
class ScriptedIntake:
    """Stands in for cloning and scanning: writes a small repository, or raises what it is given.

    ``failures`` are raised one for each call, before anything is written; an entry that is a
    function is called with the folder and its result raised, for an error that names it.
    """

    failures: list[Exception | Callable[[Path], Exception]] = field(default_factory=list)
    ref: str | None = "main"

    def __post_init__(self) -> None:
        self.cloned: list[tuple[str, Path, str | None]] = []  # source, folder, commit asked for

    def __call__(self, source: str, clone_into: Path, commit: str | None) -> IntakeResult:
        self.cloned.append((source, clone_into, commit))
        if self.failures:
            failure = self.failures.pop(0)
            raise failure if isinstance(failure, Exception) else failure(clone_into)
        clone_into.mkdir()
        (clone_into / "README.md").write_text(README)
        snapshot = Snapshot(
            root=clone_into, source_url=source, commit_sha=commit or SCRIPTED_COMMIT, ref=self.ref
        )
        return IntakeResult(snapshot=snapshot, files=RepoFiles(clone_into), findings=[])


def store_one_checkpoint(checkpoints: RunCheckpoints) -> None:
    checkpoints.store.put(
        {"configurable": {"thread_id": checkpoints.thread_id, "checkpoint_ns": ""}},
        empty_checkpoint(),
        {"source": "loop", "step": 1},
        {},
    )


@pytest.fixture
def leave_a_checkpoint() -> Callable[[RunCheckpoints], None]:
    """For a test that needs a thread with something in it and no run behind it."""
    return store_one_checkpoint


def graph_run(run: AnalyzeRepositoryInput) -> GraphRunRef:
    return GraphRunRef(kind=ANALYSIS_RUN_KIND, run_id=run.run_id)


@dataclass
class Analyzer:
    """The analyzer wired for a test: what it is made of, and its activities as plain functions.

    The activities are the ones the worker registers, found by name and run in Temporal's
    activity environment, which needs no server. ``to_the_end`` orders them as the workflow's
    driver does.
    """

    parts: wiring.AnalyzerParts
    registration: AgentRegistration = field(init=False)

    def __post_init__(self) -> None:
        self.registration = wiring.registration(self.parts)

    @property
    def clones(self) -> LocalClones:
        return self.parts.clones

    def activity(self, name: str) -> Callable[..., Any]:
        (found,) = (a for a in self.registration.activities if a.__name__ == name)
        return found

    def start(self, run: AnalyzeRepositoryInput) -> AnalysisPolicy:
        return ActivityEnvironment().run(self.activity(steps.START_RUN), run)

    def record_failure(self, run: AnalyzeRepositoryInput, error: str) -> None:
        ActivityEnvironment().run(
            self.activity(steps.RECORD_FAILURE), steps.RunFailure(run=run, error=error)
        )

    async def step(
        self, run: AnalyzeRepositoryInput, resume: ActionOutcome | None = None
    ) -> StepOutcome:
        return await ActivityEnvironment().run(
            self.activity(RUN_GRAPH_STEP), StepInput(run=graph_run(run), resume=resume)
        )

    async def act(self, request: ActionRequest) -> ActionOutcome:
        return await ActivityEnvironment().run(self.activity(RUN_ACTION), request)

    async def forget(self, run: AnalyzeRepositoryInput) -> None:
        await ActivityEnvironment().run(self.activity(FORGET_RUN), graph_run(run))

    async def cloned(self, run: AnalyzeRepositoryInput) -> ActionOutcome:
        """Take the run's first step and run the clone it asks for; what came of the clone."""
        asked = (await self.step(run)).action
        assert asked is not None
        return await self.act(asked)

    async def to_the_end(self, run: AnalyzeRepositoryInput) -> dict[str, Any]:
        """Take the run's steps and its clone, in the workflow's order; the last step's output."""
        output = (await self.step(run, await self.cloned(run))).output
        assert output is not None
        return output


@pytest.fixture(autouse=True)
def temporary_folder(tmp_path: Path, monkeypatch) -> Path:
    """The system's temporary folder, for the length of a test.

    The worker keeps its clones under it, and sweeps them when it is wired: no test does
    that to the folder of the machine it runs on.
    """
    folder = tmp_path / "system-temporary-folder"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


@pytest.fixture
def model() -> ScriptedModel:
    """A model that submits the profile at once."""
    return ScriptedModel()


@pytest.fixture
def intake() -> ScriptedIntake:
    """An intake that succeeds."""
    return ScriptedIntake()


@pytest.fixture
def scripted_intake() -> type[ScriptedIntake]:
    """For tests that script their own intake."""
    return ScriptedIntake


@pytest.fixture
def clones_root(tmp_path: Path) -> Path:
    return tmp_path / "clones"


@pytest.fixture
def analyzer_with(sessions, checkpoints, clones_root, model, intake):
    """The analyzer with the given parts in place of the fixtures' own."""

    def analyzer(
        *,
        model: ScriptedModel = model,
        intake: Intake = intake,
        reaching: CheckpointAccess = checkpoints,
        policy: AnalysisPolicy = TEST_POLICY,
        clones: LocalClones | None = None,
        new_model: Callable[[], ScriptedModel] | None = None,
    ) -> Analyzer:
        return Analyzer(
            wiring.AnalyzerParts(
                sessions=sessions,
                checkpoints=reaching,
                clones=clones or LocalClones(clones_root, intake),
                new_model=new_model or (lambda: model),
                extractors=[],
                policy=policy,
            )
        )

    return analyzer


@pytest.fixture
def analyzer(analyzer_with) -> Analyzer:
    return analyzer_with()


@pytest.fixture
def started(analyzer, run) -> AnalyzeRepositoryInput:
    """A run that a worker has picked up."""
    analyzer.start(run)
    return run


def intake_of(source: Path) -> Intake:
    """Real intake (git, the secret scan) of the repository at ``source``, whatever URL is named."""

    def intake(repository_url: str, clone_into: Path, commit: str | None) -> IntakeResult:
        return run_intake(str(source), clone_into, commit=commit)

    return intake


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
def intake_of_a_local_repository(source_repo) -> Intake:
    """Real intake of ``source_repo``."""
    return intake_of(source_repo)


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
    return checkpoints_in(engine.url.render_as_string(hide_password=False))


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

    def snapshots(self, run: AnalyzeRepositoryInput) -> list[tuple[str, str | None]]:
        """The commit and the branch of each snapshot stored for the run's project."""
        with transaction(self.sessions) as session:
            stored = session.execute(
                select(RepoSnapshot.commit_sha, RepoSnapshot.ref).where(
                    RepoSnapshot.project_id == run.project_id
                )
            )
            return [(commit_sha, ref) for commit_sha, ref in stored]

    def has_checkpoints(self, run: AnalyzeRepositoryInput) -> bool:
        """Whether any checkpoint table holds a row of the analysis run's thread."""
        return self.thread_has_checkpoints(run_thread_id(run.run_id))

    def thread_has_checkpoints(self, thread_id: str) -> bool:
        """Whether any checkpoint table holds a row of the thread, of a run of any kind."""
        thread = {"thread": thread_id}
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
