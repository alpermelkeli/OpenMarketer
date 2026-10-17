"""Tests for requesting a studio run: the stored run and the workflow started for it.

They need PostgreSQL (see the root ``conftest.py``). The request commits its own
transactions, so rows are committed here too. No workflow engine is used: starting a workflow
is a function the test supplies.
"""

import uuid

import pytest
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.models import Project, StudioRunStatus, Workspace
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.db.studio_runs import StudioRun, studio_run
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.request import (
    REQUESTED_BY_THE_CHAT,
    WORKFLOW_NOT_STARTED,
    StudioNotStarted,
    request_asset,
)
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.studio.workflow_contract import (
    STUDIO_TASK_QUEUE,
    STUDIO_WORKFLOW,
    StudioRunInput,
    studio_workflow_id,
)
from openmarketer_core.workflow_start import WorkflowNotStarted, WorkflowStart

REQUEST = AssetRequest(goal="Announce the launch", platform=Platform.OG_IMAGE)


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
        project = Project(
            workspace_id=workspace_id, name="App", source_repo_url="https://example.com/a.git"
        )
        session.add(project)
        session.flush()
        return project.id


def stored(sessions, workspace_id: uuid.UUID, run_id: uuid.UUID, project_id) -> StudioRun:
    with transaction(sessions) as session:
        return studio_run(session, workspace_id=workspace_id, project_id=project_id, run_id=run_id)


def unreachable(_: WorkflowStart) -> None:
    raise WorkflowNotStarted("connection refused")


def test_a_requested_run_is_queued_and_stored(sessions, workspace_id, project_id):
    run = request_asset(
        sessions,
        workspace_id=workspace_id,
        project_id=project_id,
        request=REQUEST,
        start_workflow=lambda _: None,
    )

    assert run.status is StudioRunStatus.QUEUED
    assert run.requested_by == "person"
    assert stored(sessions, workspace_id, run.id, project_id) == run


def test_the_workflow_is_started_for_the_stored_run(sessions, workspace_id, project_id):
    started: list[WorkflowStart] = []

    run = request_asset(
        sessions,
        workspace_id=workspace_id,
        project_id=project_id,
        request=REQUEST,
        start_workflow=started.append,
        requested_by=REQUESTED_BY_THE_CHAT,
    )

    assert started == [
        WorkflowStart(
            workflow=STUDIO_WORKFLOW,
            workflow_id=studio_workflow_id(run.id),
            task_queue=STUDIO_TASK_QUEUE,
            input=StudioRunInput(workspace_id=workspace_id, project_id=project_id, run_id=run.id),
        )
    ]
    assert run.requested_by == REQUESTED_BY_THE_CHAT


def test_a_run_whose_workflow_cannot_start_is_failed_at_once(sessions, workspace_id, project_id):
    with pytest.raises(StudioNotStarted) as refused:
        request_asset(
            sessions,
            workspace_id=workspace_id,
            project_id=project_id,
            request=REQUEST,
            start_workflow=unreachable,
        )

    run = stored(sessions, workspace_id, refused.value.run_id, project_id)
    assert run.status is StudioRunStatus.FAILED
    assert run.error == WORKFLOW_NOT_STARTED


def test_no_workflow_starts_for_a_project_the_workspace_does_not_have(sessions, workspace_id):
    started: list[WorkflowStart] = []

    with pytest.raises(ProjectNotFound):
        request_asset(
            sessions,
            workspace_id=workspace_id,
            project_id=uuid.uuid4(),
            request=REQUEST,
            start_workflow=started.append,
        )
    assert started == []
