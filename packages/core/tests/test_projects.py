"""Tests for the project store. They need PostgreSQL (see the root ``conftest.py``)."""

import uuid

import pytest
from sqlalchemy.orm import Session

from openmarketer_core.db.models import Project, Workspace
from openmarketer_core.db.projects import (
    ProjectAlreadyExists,
    ProjectNotFound,
    create_project,
    get_project,
)
from openmarketer_core.intake import IntakeError

REPOSITORY = "https://example.com/acme/app.git"


def new_workspace(session: Session) -> uuid.UUID:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    return workspace.id


@pytest.fixture
def workspace_id(session) -> uuid.UUID:
    return new_workspace(session)


def test_created_project_is_stored_in_the_workspace(session, workspace_id):
    created = create_project(
        session, workspace_id=workspace_id, name="Example App", source_repo_url=REPOSITORY
    )
    row = session.get_one(Project, created.id)
    assert (row.workspace_id, row.name, row.source_repo_url) == (
        workspace_id,
        "Example App",
        REPOSITORY,
    )


def test_created_project_carries_when_it_was_created(session, workspace_id):
    created = create_project(
        session, workspace_id=workspace_id, name="Example App", source_repo_url=REPOSITORY
    )
    assert created.created_at == session.get_one(Project, created.id).created_at


@pytest.mark.parametrize("source", ["/etc", "file:///etc", "http://example.com/repo.git"])
def test_project_needs_an_https_repository(session, workspace_id, source):
    with pytest.raises(IntakeError):
        create_project(session, workspace_id=workspace_id, name="App", source_repo_url=source)


def test_repository_can_have_only_one_project_in_a_workspace(session, workspace_id):
    create_project(session, workspace_id=workspace_id, name="First", source_repo_url=REPOSITORY)
    with pytest.raises(ProjectAlreadyExists):
        create_project(
            session, workspace_id=workspace_id, name="Second", source_repo_url=REPOSITORY
        )


def test_two_workspaces_can_have_a_project_for_the_same_repository(session, workspace_id):
    create_project(session, workspace_id=workspace_id, name="Ours", source_repo_url=REPOSITORY)
    theirs = create_project(
        session, workspace_id=new_workspace(session), name="Theirs", source_repo_url=REPOSITORY
    )
    assert theirs.name == "Theirs"


def test_project_is_read_back_inside_its_workspace(session, workspace_id):
    created = create_project(
        session, workspace_id=workspace_id, name="Example App", source_repo_url=REPOSITORY
    )
    assert get_project(session, workspace_id=workspace_id, project_id=created.id) == created


def test_project_of_another_workspace_is_not_found(session, workspace_id):
    created = create_project(
        session, workspace_id=workspace_id, name="Example App", source_repo_url=REPOSITORY
    )
    with pytest.raises(ProjectNotFound):
        get_project(session, workspace_id=new_workspace(session), project_id=created.id)


def test_unknown_project_is_not_found(session, workspace_id):
    with pytest.raises(ProjectNotFound):
        get_project(session, workspace_id=workspace_id, project_id=uuid.uuid4())
