"""Tests for creating, reading and listing projects.

They need PostgreSQL (see the root ``conftest.py``).
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from openmarketer_api.dependencies import db_session
from openmarketer_api.identity import LOCAL_USER_ID
from openmarketer_core.db.analysis_runs import mark_run_failed, request_analysis_run
from openmarketer_core.db.evidence_store import local_workspace_id, save_analysis_of_project
from openmarketer_core.db.models import Project, Workspace
from openmarketer_core.db.profile_versions import approve_profile_version
from openmarketer_core.profile import ProductProfile
from openmarketer_core.repository_analysis.intake import Snapshot

REPOSITORY = "https://example.com/acme/app.git"
PROFILE = ProductProfile.model_validate({"product": {"name": "Example App", "type": "dev_tool"}})
NOON = datetime(2026, 10, 1, 12, tzinfo=UTC)
PROJECT_FIELDS = {
    "id",
    "name",
    "repository_url",
    "created_at",
    "latest_draft_version",
    "latest_approved_version",
    "unfinished_run_id",
}


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
    assert set(body) == PROJECT_FIELDS


def test_created_project_has_no_review_state_yet(client):
    body = create(client).json()
    assert (
        body["latest_draft_version"],
        body["latest_approved_version"],
        body["unfinished_run_id"],
    ) == (None, None, None)


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


def test_second_project_for_the_same_repository_is_a_conflict_naming_the_first(client):
    first = create(client).json()
    response = create(client, name="Again")
    assert response.status_code == 409
    assert response.json() == {
        "code": "project_already_exists",
        "message": f"a project for {REPOSITORY} already exists",
        "existing_project_id": first["id"],
    }


def test_project_named_by_a_conflict_can_be_opened(client):
    create(client)
    existing = create(client, name="Again").json()["existing_project_id"]
    assert client.get(f"/v1/projects/{existing}").json()["name"] == "Example App"


def test_unknown_field_is_rejected(client):
    assert create(client, workspace_id=str(uuid.uuid4())).status_code == 422


def test_blank_name_is_rejected(client):
    assert create(client, name="   ").status_code == 422


# ------------------------------------------------------------------- read
def elsewhere(session: Session) -> uuid.UUID:
    """Another workspace than the one requests work in."""
    workspace = Workspace(name="Elsewhere")
    session.add(workspace)
    session.flush()
    return workspace.id


def project_created(
    session: Session, minutes_ago: int, workspace_id: uuid.UUID | None = None
) -> str:
    """A project with a creation time of its own; the rows of one test share a transaction."""
    project = Project(
        workspace_id=workspace_id or local_workspace_id(session),
        name=f"App of {minutes_ago} minutes ago",
        source_repo_url=f"https://example.com/acme/{uuid.uuid4()}.git",
        created_at=NOON - timedelta(minutes=minutes_ago),
    )
    session.add(project)
    session.flush()
    return str(project.id)


def analysed(session: Session, project_id: str) -> None:
    """Store an analysis of the project as its next draft version."""
    snapshot = Snapshot(
        root=Path("/unused"), source_url="https://unused", commit_sha="a" * 40, ref="main"
    )
    save_analysis_of_project(
        session,
        workspace_id=local_workspace_id(session),
        project_id=uuid.UUID(project_id),
        snapshot=snapshot,
        facts=[],
        profile=PROFILE,
    )


def test_project_is_read_back_as_it_was_created(client):
    created = create(client).json()
    response = client.get(f"/v1/projects/{created['id']}")
    assert (response.status_code, response.json()) == (200, created)


def test_project_names_its_latest_draft_and_approved_version(client, session):
    project_id = project_created(session, minutes_ago=10)
    analysed(session, project_id)
    approve_profile_version(
        session,
        workspace_id=local_workspace_id(session),
        project_id=uuid.UUID(project_id),
        version=1,
        approved_by=LOCAL_USER_ID,
    )
    analysed(session, project_id)
    body = client.get(f"/v1/projects/{project_id}").json()
    assert (body["latest_draft_version"], body["latest_approved_version"]) == (2, 1)


def test_project_names_its_unfinished_run_until_the_run_ends(client, session):
    project_id = project_created(session, minutes_ago=10)
    scope = {"workspace_id": local_workspace_id(session), "project_id": uuid.UUID(project_id)}
    run = request_analysis_run(session, **scope)
    assert client.get(f"/v1/projects/{project_id}").json()["unfinished_run_id"] == str(run.id)
    mark_run_failed(session, **scope, run_id=run.id, error="clone failed")
    assert client.get(f"/v1/projects/{project_id}").json()["unfinished_run_id"] is None


def test_unknown_project_is_not_found(client):
    project_id = uuid.uuid4()
    response = client.get(f"/v1/projects/{project_id}")
    assert (response.status_code, response.json()) == (
        404,
        {"code": "project_not_found", "message": f"project {project_id} not found"},
    )


def test_project_of_another_workspace_is_answered_like_an_unknown_one(client, session):
    foreign = project_created(session, minutes_ago=10, workspace_id=elsewhere(session))
    response = client.get(f"/v1/projects/{foreign}")
    assert (response.status_code, response.json()) == (
        404,
        {"code": "project_not_found", "message": f"project {foreign} not found"},
    )


def test_malformed_project_identifier_is_rejected(client):
    assert client.get("/v1/projects/not-a-uuid").status_code == 422


# ------------------------------------------------------------------- list
def listed(client, **query) -> list[str]:
    return [
        project["id"] for project in client.get("/v1/projects", params=query).json()["projects"]
    ]


def test_workspace_without_projects_lists_none(client):
    response = client.get("/v1/projects")
    assert (response.status_code, response.json()) == (200, {"projects": [], "next_cursor": None})


def test_projects_are_listed_newest_first(client, session):
    old = project_created(session, minutes_ago=30)
    new = project_created(session, minutes_ago=10)
    assert listed(client) == [new, old]


def test_listed_project_is_the_project_as_it_is_read_alone(client, session):
    project_id = project_created(session, minutes_ago=10)
    analysed(session, project_id)
    request_analysis_run(
        session, workspace_id=local_workspace_id(session), project_id=uuid.UUID(project_id)
    )
    alone = client.get(f"/v1/projects/{project_id}").json()
    assert client.get("/v1/projects").json()["projects"] == [alone]
    assert (alone["latest_draft_version"], alone["unfinished_run_id"] is not None) == (1, True)


def test_projects_of_another_workspace_are_not_listed(client, session):
    ours = project_created(session, minutes_ago=10)
    project_created(session, minutes_ago=5, workspace_id=elsewhere(session))
    assert listed(client) == [ours]


def test_list_holds_no_more_projects_than_the_limit_and_names_the_next_page(client, session):
    projects = [project_created(session, minutes_ago=n) for n in range(1, 4)]
    page = client.get("/v1/projects", params={"limit": 2}).json()
    assert [project["id"] for project in page["projects"]] == projects[:2]
    assert listed(client, limit=2, cursor=page["next_cursor"]) == projects[2:]


def test_last_page_of_projects_has_no_next_cursor(client, session):
    for n in range(1, 3):
        project_created(session, minutes_ago=n)
    assert client.get("/v1/projects", params={"limit": 2}).json()["next_cursor"] is None


def test_project_created_between_two_pages_neither_repeats_nor_hides_one(client, session):
    projects = [project_created(session, minutes_ago=n) for n in range(1, 5)]
    first = client.get("/v1/projects", params={"limit": 2}).json()
    create(client)
    second = listed(client, limit=2, cursor=first["next_cursor"])
    assert [project["id"] for project in first["projects"]] + second == projects


def test_list_without_a_limit_holds_fifty_projects(client, session):
    for n in range(51):
        project_created(session, minutes_ago=n)
    page = client.get("/v1/projects").json()
    assert (len(page["projects"]), page["next_cursor"] is not None) == (50, True)


@pytest.mark.parametrize("limit", [0, 101, "all"])
def test_limit_outside_the_bounds_is_rejected(client, limit):
    assert client.get("/v1/projects", params={"limit": limit}).status_code == 422


def test_largest_limit_is_accepted(client):
    assert client.get("/v1/projects", params={"limit": 100}).status_code == 200
