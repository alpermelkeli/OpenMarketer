"""Tests for analysis runs executed in a background thread of the API process.

The analysis itself is replaced by a fixed result or a fixed failure. They need
PostgreSQL (see the root ``conftest.py``): a run commits its own transaction.
"""

import threading
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_api.analysis_runs import (
    AnalysisAlreadyRunning,
    AnalysisRun,
    AnalysisRunNotFound,
    AnalysisStatus,
)
from openmarketer_api.dependencies import Services
from openmarketer_api.identity import current_workspace_id
from openmarketer_api.in_process_runs import InProcessAnalysisRuns
from openmarketer_core.analyzer import Analysis
from openmarketer_core.db.models import ProductProfileRecord, Project, Workspace
from openmarketer_core.db.projects import ProjectNotFound, create_project
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.extraction import ExtractionResult
from openmarketer_core.intake import IntakeError, IntakeResult, RepoFiles, Snapshot
from openmarketer_core.profile import ProductProfile
from openmarketer_core.repository_analysis import RepositoryAnalysis

PROFILE = ProductProfile.model_validate({"product": {"name": "Example App", "type": "dev_tool"}})


def analysed(repository_url: str, clone_into: Path) -> RepositoryAnalysis:
    """What a successful analysis of any repository returns in these tests."""
    clone_into.mkdir()
    snapshot = Snapshot(root=clone_into, source_url=repository_url, commit_sha="a" * 40, ref="main")
    return RepositoryAnalysis(
        intake=IntakeResult(snapshot=snapshot, files=RepoFiles(clone_into), findings=[]),
        extraction=ExtractionResult(),
        analysis=Analysis(profile=PROFILE, cost_usd=0.0, steps=1, model="scripted"),
    )


def failing_with(error: Exception):
    def analyse(repository_url: str, clone_into: Path) -> RepositoryAnalysis:
        raise error

    return analyse


@pytest.fixture
def sessions(engine) -> sessionmaker[Session]:
    return session_factory(engine.url.render_as_string(hide_password=False))


@pytest.fixture
def workspace_id(sessions) -> uuid.UUID:
    with transaction(sessions) as session:
        workspace = Workspace(name="Acme")
        session.add(workspace)
        session.flush()
        return workspace.id


@pytest.fixture
def project_id(sessions, workspace_id) -> uuid.UUID:
    with transaction(sessions) as session:
        return create_project(
            session,
            workspace_id=workspace_id,
            name="Example App",
            source_repo_url="https://example.com/acme/app.git",
        ).id


def start(runs: InProcessAnalysisRuns, sessions, workspace_id, project_id) -> AnalysisRun:
    with transaction(sessions) as session:
        return runs.start(session, workspace_id=workspace_id, project_id=project_id)


def finished(runs: InProcessAnalysisRuns, run: AnalysisRun) -> AnalysisRun:
    """The run once its thread has ended; fails the test if that takes too long."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = runs.get(workspace_id=run.workspace_id, project_id=run.project_id, run_id=run.id)
        if current.finished_at is not None:
            return current
        time.sleep(0.01)
    raise AssertionError("the run did not finish")


def test_start_returns_while_the_analysis_is_still_running(sessions, workspace_id, project_id):
    may_finish = threading.Event()

    def slow(repository_url: str, clone_into: Path) -> RepositoryAnalysis:
        may_finish.wait(timeout=10)
        return analysed(repository_url, clone_into)

    runs = InProcessAnalysisRuns(sessions, slow)
    run = start(runs, sessions, workspace_id, project_id)
    still_open = runs.get(workspace_id=workspace_id, project_id=project_id, run_id=run.id)
    may_finish.set()
    assert run.status is AnalysisStatus.QUEUED
    assert still_open.finished_at is None
    assert finished(runs, run).status is AnalysisStatus.SUCCEEDED


def test_succeeded_run_stores_a_draft_and_names_its_version(sessions, workspace_id, project_id):
    runs = InProcessAnalysisRuns(sessions, analysed)
    done = finished(runs, start(runs, sessions, workspace_id, project_id))
    with sessions() as session:
        stored = session.scalars(
            select(ProductProfileRecord).where(ProductProfileRecord.project_id == project_id)
        ).one()
    assert (done.status, done.profile_version, done.error) == (AnalysisStatus.SUCCEEDED, 1, None)
    assert (stored.version, stored.status.value, stored.approved_by) == (1, "draft", None)


def test_draft_of_a_succeeded_run_is_the_one_the_profile_route_returns(
    app, client: TestClient, sessions, workspace_id, project_id
):
    runs = InProcessAnalysisRuns(sessions, analysed)
    app.state.services = Services(sessions=sessions, analysis_runs=runs)
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    started = client.post(f"/v1/projects/{project_id}/analyses").json()
    run_id = uuid.UUID(started["id"])
    finished(runs, runs.get(workspace_id=workspace_id, project_id=project_id, run_id=run_id))
    run = client.get(f"/v1/projects/{project_id}/analyses/{run_id}").json()
    draft = client.get(f"/v1/projects/{project_id}/profile/draft").json()
    assert (run["status"], run["profile_version"]) == ("succeeded", draft["version"])
    assert ProductProfile.model_validate(draft["profile"]) == PROFILE


def test_analysis_is_given_the_repository_of_the_project(sessions, workspace_id, project_id):
    analysed_urls: list[str] = []

    def recording(repository_url: str, clone_into: Path) -> RepositoryAnalysis:
        analysed_urls.append(repository_url)
        return analysed(repository_url, clone_into)

    runs = InProcessAnalysisRuns(sessions, recording)
    finished(runs, start(runs, sessions, workspace_id, project_id))
    assert analysed_urls == ["https://example.com/acme/app.git"]


def test_failed_analysis_ends_the_run_with_its_reason(sessions, workspace_id, project_id):
    runs = InProcessAnalysisRuns(sessions, failing_with(IntakeError("git clone failed: not found")))
    done = finished(runs, start(runs, sessions, workspace_id, project_id))
    assert (done.status, done.error, done.profile_version) == (
        AnalysisStatus.FAILED,
        "git clone failed: not found",
        None,
    )


def test_reason_of_a_failure_does_not_name_the_clone_folder(sessions, workspace_id, project_id):
    def analyse(repository_url: str, clone_into: Path) -> RepositoryAnalysis:
        raise IntakeError(f"git clone failed: Cloning into '{clone_into}'... not found")

    runs = InProcessAnalysisRuns(sessions, analyse)
    done = finished(runs, start(runs, sessions, workspace_id, project_id))
    assert done.error == "git clone failed: Cloning into '<clone>/repo'... not found"


def test_unexpected_error_ends_the_run_without_its_detail(sessions, workspace_id, project_id):
    runs = InProcessAnalysisRuns(sessions, failing_with(RuntimeError("/Users/someone/secret")))
    done = finished(runs, start(runs, sessions, workspace_id, project_id))
    assert (done.status, done.error) == (
        AnalysisStatus.FAILED,
        "the analysis stopped unexpectedly",
    )


def test_project_is_analysed_once_at_a_time(sessions, workspace_id, project_id):
    may_finish = threading.Event()

    def slow(repository_url: str, clone_into: Path) -> RepositoryAnalysis:
        may_finish.wait(timeout=10)
        return analysed(repository_url, clone_into)

    runs = InProcessAnalysisRuns(sessions, slow)
    first = start(runs, sessions, workspace_id, project_id)
    with pytest.raises(AnalysisAlreadyRunning):
        start(runs, sessions, workspace_id, project_id)
    may_finish.set()
    finished(runs, first)


def test_project_can_be_analysed_again_after_a_run_has_finished(sessions, workspace_id, project_id):
    runs = InProcessAnalysisRuns(sessions, analysed)
    finished(runs, start(runs, sessions, workspace_id, project_id))
    again = finished(runs, start(runs, sessions, workspace_id, project_id))
    assert again.profile_version == 2


def test_unknown_project_is_not_analysed(sessions, workspace_id):
    runs = InProcessAnalysisRuns(sessions, analysed)
    with pytest.raises(ProjectNotFound):
        start(runs, sessions, workspace_id, uuid.uuid4())


def test_project_of_a_local_folder_is_not_analysed(sessions, workspace_id):
    """The command line stores such projects; a request must not make the service read the disk."""
    with transaction(sessions) as session:
        local = Project(workspace_id=workspace_id, name="Local", source_repo_url="file:///etc")
        session.add(local)
        session.flush()
        local_id = local.id
    runs = InProcessAnalysisRuns(sessions, analysed)
    with pytest.raises(IntakeError, match="must start with https://"):
        start(runs, sessions, workspace_id, local_id)
    with sessions() as session:
        stored = session.scalar(
            select(func.count())
            .select_from(ProductProfileRecord)
            .where(ProductProfileRecord.project_id == local_id)
        )
    assert stored == 0


def test_run_is_not_found_from_another_workspace(sessions, workspace_id, project_id):
    runs = InProcessAnalysisRuns(sessions, analysed)
    run = finished(runs, start(runs, sessions, workspace_id, project_id))
    with pytest.raises(AnalysisRunNotFound):
        runs.get(workspace_id=uuid.uuid4(), project_id=project_id, run_id=run.id)
