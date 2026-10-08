"""Tests for creating a project. They need PostgreSQL (see the root ``conftest.py``)."""

import uuid

import pytest
from sqlalchemy import select

from openmarketer_api.dependencies import db_session
from openmarketer_core.db.evidence_store import local_workspace_id
from openmarketer_core.db.models import Project

REPOSITORY = "https://example.com/acme/app.git"


@pytest.fixture(autouse=True)
def database(app, session) -> None:
    """Requests work in the test's session, which is rolled back afterwards."""
    app.dependency_overrides[db_session] = lambda: session


def create(client, **changes):
    body = {"name": "Example App", "repository_url": REPOSITORY} | changes
    return client.post("/v1/projects", json=body)


def test_created_project_is_returned(client):
    response = create(client)
    assert response.status_code == 201
    body = response.json()
    assert (body["name"], body["repository_url"]) == ("Example App", REPOSITORY)
    assert set(body) == {"id", "name", "repository_url", "created_at"}


def test_created_project_is_stored_in_the_local_workspace(client, session):
    project_id = uuid.UUID(create(client).json()["id"])
    stored = session.scalar(select(Project.workspace_id).where(Project.id == project_id))
    assert stored == local_workspace_id(session)


@pytest.mark.parametrize(
    "repository_url",
    ["/etc", "file:///etc", "http://example.com/repo.git", "https://user:secret@example.com/r.git"],
)
def test_repository_must_be_a_plain_https_url(client, session, repository_url):
    response = create(client, repository_url=repository_url)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_repository_url"
    assert session.scalar(select(Project.id)) is None


def test_second_project_for_the_same_repository_is_a_conflict(client):
    create(client)
    response = create(client, name="Again")
    assert response.status_code == 409
    assert response.json() == {
        "code": "project_already_exists",
        "message": f"a project for {REPOSITORY} already exists",
    }


def test_unknown_field_is_rejected(client):
    assert create(client, workspace_id=str(uuid.uuid4())).status_code == 422


def test_blank_name_is_rejected(client):
    assert create(client, name="   ").status_code == 422
