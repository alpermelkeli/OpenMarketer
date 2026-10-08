"""Tests for requesting an analysis: the stored run and the workflow started for it.

They need PostgreSQL (see the root ``conftest.py``). The request commits its
own transactions, so rows are committed here too. No workflow engine is used:
starting a workflow is a function the test supplies.
"""

import uuid

import pytest
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.analysis_request import (
    WORKFLOW_NOT_STARTED,
    AnalysisNotStarted,
    WorkflowNotStarted,
    request_analysis,
)
from openmarketer_core.analysis_workflow import AnalyzeRepositoryInput
from openmarketer_core.db.analysis_runs import AnalysisAlreadyRunning, AnalysisRun, analysis_run
from openmarketer_core.db.models import AnalysisRunStatus, Project, Workspace
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.intake import IntakeError


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


def new_project(sessions, workspace_id: uuid.UUID, repository_url: str) -> uuid.UUID:
    with transaction(sessions) as session:
        project = Project(workspace_id=workspace_id, name="App", source_repo_url=repository_url)
        session.add(project)
        session.flush()
        return project.id


@pytest.fixture
def project_id(sessions, workspace_id) -> uuid.UUID:
    return new_project(sessions, workspace_id, "https://example.com/acme/app.git")


def stored(sessions, workspace_id: uuid.UUID, run: AnalysisRun) -> AnalysisRun:
    with transaction(sessions) as session:
        return analysis_run(
            session, workspace_id=workspace_id, project_id=run.project_id, run_id=run.id
        )


def unreachable(_: AnalyzeRepositoryInput) -> None:
    raise WorkflowNotStarted("connection refused")


def test_requested_run_is_queued(sessions, workspace_id, project_id):
    run = request_analysis(
        sessions, workspace_id=workspace_id, project_id=project_id, start_workflow=lambda _: None
    )
    assert run.status is AnalysisRunStatus.QUEUED
    assert stored(sessions, workspace_id, run) == run


def test_workflow_is_started_for_the_stored_run(sessions, workspace_id, project_id):
    started: list[AnalyzeRepositoryInput] = []
    run = request_analysis(
        sessions, workspace_id=workspace_id, project_id=project_id, start_workflow=started.append
    )
    assert started == [
        AnalyzeRepositoryInput(workspace_id=workspace_id, project_id=project_id, run_id=run.id)
    ]


def test_run_is_committed_before_its_workflow_is_started(sessions, workspace_id, project_id):
    seen_by_the_worker: list[AnalysisRunStatus] = []

    def start(workflow: AnalyzeRepositoryInput) -> None:
        with transaction(sessions) as session:
            run = analysis_run(
                session,
                workspace_id=workflow.workspace_id,
                project_id=workflow.project_id,
                run_id=workflow.run_id,
            )
        seen_by_the_worker.append(run.status)

    request_analysis(
        sessions, workspace_id=workspace_id, project_id=project_id, start_workflow=start
    )
    assert seen_by_the_worker == [AnalysisRunStatus.QUEUED]


def test_run_whose_workflow_cannot_be_started_is_recorded_as_failed(
    sessions, workspace_id, project_id
):
    with pytest.raises(AnalysisNotStarted) as refused:
        request_analysis(
            sessions, workspace_id=workspace_id, project_id=project_id, start_workflow=unreachable
        )
    with transaction(sessions) as session:
        run = analysis_run(
            session,
            workspace_id=workspace_id,
            project_id=project_id,
            run_id=refused.value.run_id,
        )
    assert (run.status, run.error) == (AnalysisRunStatus.FAILED, WORKFLOW_NOT_STARTED)


def test_project_is_not_blocked_by_a_run_that_was_never_started(sessions, workspace_id, project_id):
    with pytest.raises(AnalysisNotStarted):
        request_analysis(
            sessions, workspace_id=workspace_id, project_id=project_id, start_workflow=unreachable
        )
    again = request_analysis(
        sessions, workspace_id=workspace_id, project_id=project_id, start_workflow=lambda _: None
    )
    assert again.status is AnalysisRunStatus.QUEUED


def test_second_request_while_a_run_is_unfinished_starts_no_workflow(
    sessions, workspace_id, project_id
):
    started: list[AnalyzeRepositoryInput] = []
    request_analysis(
        sessions, workspace_id=workspace_id, project_id=project_id, start_workflow=started.append
    )
    with pytest.raises(AnalysisAlreadyRunning):
        request_analysis(
            sessions,
            workspace_id=workspace_id,
            project_id=project_id,
            start_workflow=started.append,
        )
    assert len(started) == 1


@pytest.mark.parametrize("repository_url", ["file:///etc", "/etc", "http://example.com/a.git"])
def test_project_that_is_not_an_https_repository_gets_no_run(
    sessions, workspace_id, repository_url
):
    started: list[AnalyzeRepositoryInput] = []
    local = new_project(sessions, workspace_id, repository_url)
    with pytest.raises(IntakeError, match="must start with https://"):
        request_analysis(
            sessions, workspace_id=workspace_id, project_id=local, start_workflow=started.append
        )
    assert started == []


def test_project_of_another_workspace_is_not_found(sessions, workspace_id, project_id):
    with transaction(sessions) as session:
        elsewhere = Workspace(name="Elsewhere")
        session.add(elsewhere)
        session.flush()
        elsewhere_id = elsewhere.id
    with pytest.raises(ProjectNotFound):
        request_analysis(
            sessions,
            workspace_id=elsewhere_id,
            project_id=project_id,
            start_workflow=lambda _: None,
        )
