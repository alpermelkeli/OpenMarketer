"""Tests for the profile review routes. They need PostgreSQL (see the root ``conftest.py``).

Each test starts from a project whose analysis left draft version 1.
"""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from openmarketer_api.dependencies import db_session
from openmarketer_api.identity import LOCAL_USER_ID
from openmarketer_core.db.evidence_store import local_workspace_id, save_analysis
from openmarketer_core.db.models import ProductProfileRecord, Workspace
from openmarketer_core.intake import Snapshot
from openmarketer_core.profile import ProductProfile

PROFILE = {"product": {"name": "Example App", "type": "dev_tool"}}
EDITED = {"product": {"name": "Example App Pro", "type": "dev_tool"}}


@pytest.fixture(autouse=True)
def database(app, session) -> None:
    """Requests work in the test's session, which is rolled back afterwards."""
    app.dependency_overrides[db_session] = lambda: session


def analysed_project(session: Session, workspace_id: uuid.UUID) -> uuid.UUID:
    """A project in the workspace whose first analysis run is stored as draft version 1."""
    snapshot = Snapshot(
        root=Path("/unused"),
        source_url="https://example.com/acme/app.git",
        commit_sha="a" * 40,
        ref="main",
    )
    saved = save_analysis(
        session,
        workspace_id=workspace_id,
        snapshot=snapshot,
        facts=[],
        profile=ProductProfile.model_validate(PROFILE),
    )
    return saved.project_id


@pytest.fixture
def project_id(session) -> uuid.UUID:
    return analysed_project(session, local_workspace_id(session))


@pytest.fixture
def profile_url(project_id) -> str:
    return f"/v1/projects/{project_id}/profile"


def stored_versions(session: Session, project_id: uuid.UUID) -> list[tuple[int, str, str]]:
    """Every stored version of the project: number, status and product name."""
    rows = session.scalars(
        select(ProductProfileRecord)
        .where(ProductProfileRecord.project_id == project_id)
        .order_by(ProductProfileRecord.version)
        .execution_options(populate_existing=True)
    )
    return [(row.version, row.status.value, row.content["product"]["name"]) for row in rows]


# ------------------------------------------------------------------- read
def test_latest_draft_is_returned_with_its_version(client, profile_url, project_id):
    response = client.get(f"{profile_url}/draft")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "project_id",
        "version",
        "status",
        "profile",
        "created_at",
        "approved_by",
        "approved_at",
    }
    assert (body["project_id"], body["version"], body["status"]) == (str(project_id), 1, "draft")
    assert (body["approved_by"], body["approved_at"]) == (None, None)
    assert ProductProfile.model_validate(body["profile"]) == ProductProfile.model_validate(PROFILE)


def test_project_without_a_draft_has_no_draft_profile(client, profile_url, project_id):
    client.post(f"{profile_url}/versions/1/approval")
    response = client.get(f"{profile_url}/draft")
    assert (response.status_code, response.json()) == (
        404,
        {"code": "profile_not_found", "message": f"project {project_id} has no draft profile"},
    )


def test_approved_profile_is_not_found_before_anything_is_approved(client, profile_url):
    response = client.get(f"{profile_url}/approved")
    assert response.status_code == 404
    assert response.json()["code"] == "profile_not_found"


def test_approved_profile_is_returned_after_approval(client, profile_url):
    client.post(f"{profile_url}/versions/1/approval")
    body = client.get(f"{profile_url}/approved").json()
    assert (body["version"], body["status"]) == (1, "approved")


def test_profile_of_a_project_in_another_workspace_is_not_found(client, session):
    elsewhere = Workspace(name="Elsewhere")
    session.add(elsewhere)
    session.flush()
    foreign_project = analysed_project(session, elsewhere.id)
    url = f"/v1/projects/{foreign_project}/profile"
    assert client.get(f"{url}/draft").status_code == 404
    assert client.post(f"{url}/versions/1/approval").status_code == 404
    assert client.post(f"{url}/versions/1/edits", json={"profile": EDITED}).status_code == 404
    assert stored_versions(session, foreign_project) == [(1, "draft", "Example App")]


# ------------------------------------------------------------------- edit
def test_edit_is_stored_as_a_new_draft_version(client, session, profile_url, project_id):
    response = client.post(f"{profile_url}/versions/1/edits", json={"profile": EDITED})
    assert response.status_code == 201
    assert (response.json()["version"], response.json()["status"]) == (2, "draft")
    assert response.json()["profile"]["product"]["name"] == "Example App Pro"
    assert stored_versions(session, project_id) == [
        (1, "draft", "Example App"),
        (2, "draft", "Example App Pro"),
    ]


def test_edit_becomes_the_latest_draft(client, profile_url):
    client.post(f"{profile_url}/versions/1/edits", json={"profile": EDITED})
    assert client.get(f"{profile_url}/draft").json()["version"] == 2


def test_edit_of_an_approved_version_leaves_it_approved(client, session, profile_url, project_id):
    client.post(f"{profile_url}/versions/1/approval")
    response = client.post(f"{profile_url}/versions/1/edits", json={"profile": EDITED})
    assert response.status_code == 201
    assert stored_versions(session, project_id) == [
        (1, "approved", "Example App"),
        (2, "draft", "Example App Pro"),
    ]


def test_edit_of_a_version_that_does_not_exist_is_not_found(
    client, session, profile_url, project_id
):
    response = client.post(f"{profile_url}/versions/7/edits", json={"profile": EDITED})
    assert (response.status_code, response.json()) == (
        404,
        {"code": "profile_not_found", "message": f"project {project_id} has no profile version 7"},
    )
    assert len(stored_versions(session, project_id)) == 1


def test_edit_that_is_not_a_valid_profile_is_rejected(client, session, profile_url, project_id):
    invalid = {"product": {"name": "Example App", "type": "Not A Slug"}}
    response = client.post(f"{profile_url}/versions/1/edits", json={"profile": invalid})
    assert response.status_code == 422
    assert len(stored_versions(session, project_id)) == 1


def test_edit_cannot_set_the_status_or_the_approver(client, session, profile_url, project_id):
    body = {"profile": EDITED, "status": "approved", "approved_by": str(uuid.uuid4())}
    assert client.post(f"{profile_url}/versions/1/edits", json=body).status_code == 422
    assert len(stored_versions(session, project_id)) == 1


def test_edit_of_an_unknown_project_is_not_found(client):
    response = client.post(
        f"/v1/projects/{uuid.uuid4()}/profile/versions/1/edits", json={"profile": EDITED}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "profile_not_found"


# ---------------------------------------------------------------- approve
def test_approval_is_recorded_under_the_current_user(client, profile_url):
    response = client.post(f"{profile_url}/versions/1/approval")
    assert response.status_code == 200
    body = response.json()
    assert (body["version"], body["status"], body["approved_by"]) == (
        1,
        "approved",
        str(LOCAL_USER_ID),
    )
    assert body["approved_at"] is not None


def test_approver_cannot_be_named_by_the_request(client, session, profile_url, project_id):
    someone_else = str(uuid.uuid4())
    client.post(
        f"{profile_url}/versions/1/approval",
        params={"approved_by": someone_else},
        json={"approved_by": someone_else},
        headers={"X-User-Id": someone_else},
    )
    approver = session.scalar(
        select(ProductProfileRecord.approved_by)
        .where(ProductProfileRecord.project_id == project_id)
        .execution_options(populate_existing=True)
    )
    assert approver == LOCAL_USER_ID


def test_approving_a_version_twice_is_a_conflict(client, profile_url, project_id):
    client.post(f"{profile_url}/versions/1/approval")
    response = client.post(f"{profile_url}/versions/1/approval")
    assert (response.status_code, response.json()) == (
        409,
        {
            "code": "profile_already_approved",
            "message": f"profile version 1 of project {project_id} is already approved",
        },
    )


def test_approving_a_version_that_does_not_exist_is_not_found(client, profile_url):
    response = client.post(f"{profile_url}/versions/7/approval")
    assert response.status_code == 404
    assert response.json()["code"] == "profile_not_found"


def test_version_zero_is_rejected(client, profile_url):
    assert client.post(f"{profile_url}/versions/0/approval").status_code == 422
