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
from openmarketer_core.db.models import ProductProfileRecord, Project, Workspace
from openmarketer_core.profile import ProductProfile
from openmarketer_core.repository_analysis.intake import Snapshot

PROFILE = {"product": {"name": "Example App", "type": "dev_tool"}}
COMMIT = "a" * 40
SUMMARY_FIELDS = {
    "project_id",
    "version",
    "status",
    "edited_from_version",
    "commit_sha",
    "created_at",
    "approved_by",
    "approved_at",
}
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
        commit_sha=COMMIT,
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
def foreign_project(session) -> uuid.UUID:
    """An analysed project of another workspace than the one requests work in."""
    elsewhere = Workspace(name="Elsewhere")
    session.add(elsewhere)
    session.flush()
    return analysed_project(session, elsewhere.id)


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
    assert set(body) == SUMMARY_FIELDS | {"profile"}
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


# ------------------------------------------------------------ one version
def test_version_is_read_by_its_number(client, profile_url):
    edited = client.post(f"{profile_url}/versions/1/edits", json={"profile": EDITED}).json()
    response = client.get(f"{profile_url}/versions/2")
    assert (response.status_code, response.json()) == (200, edited)


def test_older_version_than_the_latest_draft_can_be_read(client, profile_url):
    client.post(f"{profile_url}/versions/1/edits", json={"profile": EDITED})
    body = client.get(f"{profile_url}/versions/1").json()
    assert (body["version"], body["profile"]["product"]["name"]) == (1, "Example App")


def test_version_stored_by_an_analysis_names_the_analysed_commit(client, profile_url):
    body = client.get(f"{profile_url}/versions/1").json()
    assert (body["commit_sha"], body["edited_from_version"]) == (COMMIT, None)


def test_edit_names_the_version_it_was_made_from_and_keeps_its_commit(client, profile_url):
    client.post(f"{profile_url}/versions/1/edits", json={"profile": EDITED})
    body = client.post(f"{profile_url}/versions/2/edits", json={"profile": EDITED}).json()
    assert (body["version"], body["edited_from_version"], body["commit_sha"]) == (3, 2, COMMIT)


def test_approved_edit_still_names_the_version_it_was_made_from(client, profile_url):
    client.post(f"{profile_url}/versions/1/edits", json={"profile": EDITED})
    client.post(f"{profile_url}/versions/2/approval")
    body = client.get(f"{profile_url}/approved").json()
    assert (body["version"], body["edited_from_version"], body["commit_sha"]) == (2, 1, COMMIT)


def test_version_that_does_not_exist_is_not_found(client, profile_url, project_id):
    response = client.get(f"{profile_url}/versions/7")
    assert (response.status_code, response.json()) == (
        404,
        {"code": "profile_not_found", "message": f"project {project_id} has no profile version 7"},
    )


def test_version_of_an_unknown_project_is_not_found(client):
    response = client.get(f"/v1/projects/{uuid.uuid4()}/profile/versions/1")
    assert (response.status_code, response.json()["code"]) == (404, "profile_not_found")


def test_version_of_a_project_in_another_workspace_is_answered_like_a_missing_one(
    client, foreign_project
):
    response = client.get(f"/v1/projects/{foreign_project}/profile/versions/1")
    assert (response.status_code, response.json()) == (
        404,
        {
            "code": "profile_not_found",
            "message": f"project {foreign_project} has no profile version 1",
        },
    )


@pytest.mark.parametrize("version", [0, 2_147_483_648, "latest"])
def test_version_number_no_project_can_have_is_not_read(client, profile_url, version):
    assert client.get(f"{profile_url}/versions/{version}").status_code == 422


# ------------------------------------------------------------------- list
def listed(client, profile_url: str, **query) -> list[int]:
    versions = client.get(f"{profile_url}/versions", params=query).json()["versions"]
    return [version["version"] for version in versions]


def test_versions_are_listed_highest_number_first(client, profile_url):
    client.post(f"{profile_url}/versions/1/edits", json={"profile": EDITED})
    response = client.get(f"{profile_url}/versions")
    assert response.status_code == 200
    assert listed(client, profile_url) == [2, 1]
    assert response.json()["next_cursor"] is None


def test_listed_version_is_the_version_without_its_profile(client, profile_url):
    client.post(f"{profile_url}/versions/1/edits", json={"profile": EDITED})
    client.post(f"{profile_url}/versions/2/approval")
    alone = client.get(f"{profile_url}/versions/2").json()
    newest = client.get(f"{profile_url}/versions").json()["versions"][0]
    assert set(newest) == SUMMARY_FIELDS
    assert newest == {field: alone[field] for field in SUMMARY_FIELDS}
    assert (newest["status"], newest["edited_from_version"], newest["approved_by"]) == (
        "approved",
        1,
        str(LOCAL_USER_ID),
    )


def test_list_holds_no_more_versions_than_the_limit_and_names_the_next_page(client, profile_url):
    for _ in range(2):
        client.post(f"{profile_url}/versions/1/edits", json={"profile": EDITED})
    page = client.get(f"{profile_url}/versions", params={"limit": 2}).json()
    assert [version["version"] for version in page["versions"]] == [3, 2]
    following = client.get(
        f"{profile_url}/versions", params={"limit": 2, "cursor": page["next_cursor"]}
    ).json()
    assert (
        [version["version"] for version in following["versions"]],
        following["next_cursor"],
    ) == (
        [1],
        None,
    )


def test_version_saved_between_two_pages_neither_repeats_nor_hides_one(client, profile_url):
    for _ in range(3):
        client.post(f"{profile_url}/versions/1/edits", json={"profile": EDITED})
    first = client.get(f"{profile_url}/versions", params={"limit": 2}).json()
    client.post(f"{profile_url}/versions/4/edits", json={"profile": EDITED})
    second = listed(client, profile_url, limit=2, cursor=first["next_cursor"])
    assert [version["version"] for version in first["versions"]] + second == [4, 3, 2, 1]


def test_project_that_was_never_analysed_lists_no_versions(client, session):
    project = Project(
        workspace_id=local_workspace_id(session),
        name="New",
        source_repo_url="https://example.com/acme/new.git",
    )
    session.add(project)
    session.flush()
    response = client.get(f"/v1/projects/{project.id}/profile/versions")
    assert (response.status_code, response.json()) == (200, {"versions": [], "next_cursor": None})


def test_versions_of_an_unknown_project_are_not_found(client):
    project_id = uuid.uuid4()
    response = client.get(f"/v1/projects/{project_id}/profile/versions")
    assert (response.status_code, response.json()) == (
        404,
        {"code": "project_not_found", "message": f"project {project_id} not found"},
    )


def test_versions_of_a_project_in_another_workspace_are_answered_like_an_unknown_one(
    client, foreign_project
):
    response = client.get(f"/v1/projects/{foreign_project}/profile/versions")
    assert (response.status_code, response.json()) == (
        404,
        {"code": "project_not_found", "message": f"project {foreign_project} not found"},
    )


@pytest.mark.parametrize("limit", [0, 101])
def test_limit_of_versions_outside_the_bounds_is_rejected(client, profile_url, limit):
    assert client.get(f"{profile_url}/versions", params={"limit": limit}).status_code == 422


@pytest.mark.parametrize("cursor", ["0", "-1", "2147483648", "1.5", "abc", "١"])
def test_cursor_that_is_no_version_position_is_rejected(client, profile_url, cursor):
    response = client.get(f"{profile_url}/versions", params={"cursor": cursor})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "cursor"]


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


@pytest.mark.parametrize("body", [{"approved_by": str(uuid.uuid4())}, {}])
def test_approval_with_a_request_body_is_rejected_and_approves_nothing(
    client, session, profile_url, project_id, body
):
    response = client.post(f"{profile_url}/versions/1/approval", json=body)
    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == "this request takes no body"
    assert stored_versions(session, project_id) == [(1, "draft", "Example App")]


def test_approver_cannot_be_named_in_the_query_or_a_header(
    client, session, profile_url, project_id
):
    someone_else = str(uuid.uuid4())
    client.post(
        f"{profile_url}/versions/1/approval",
        params={"approved_by": someone_else},
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


@pytest.mark.parametrize("version", [2_147_483_648, 99999999999999999999])
def test_version_number_no_project_can_have_is_rejected_before_the_database(
    client, profile_url, version
):
    body = {"profile": PROFILE}
    assert client.post(f"{profile_url}/versions/{version}/approval").status_code == 422
    assert client.post(f"{profile_url}/versions/{version}/edits", json=body).status_code == 422


def test_highest_version_number_a_project_can_have_is_looked_up(client, profile_url):
    assert client.post(f"{profile_url}/versions/2147483647/approval").status_code == 404
