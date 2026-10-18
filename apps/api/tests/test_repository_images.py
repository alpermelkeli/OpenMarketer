"""Tests for the routes of the product's images an analysis found in the repository.

They need PostgreSQL (see the root ``conftest.py``). Each test works in a workspace of its
own, committed, since a route commits its own transaction. Images are kept with the core
function the worker calls; their bytes live in the fake asset store.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_api.dependencies import Services
from openmarketer_api.identity import current_workspace_id
from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.db.analysis_runs import mark_run_started, request_analysis_run
from openmarketer_core.db.models import Project, Workspace
from openmarketer_core.db.repository_images import (
    NewRepositoryImage,
    keep_repository_images,
    repository_image_key,
)
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.repository_analysis.repository_images.selection import (
    RepositoryImageKind,
)

PNG = b"\x89PNG\r\n\x1a\n made-up image bytes"


class NoWorkflows:
    async def start(self, workflow) -> None:
        raise AssertionError("these tests start no workflow")

    async def tell_decided(self, workflow_id: str, proposal_id: uuid.UUID) -> None:
        raise AssertionError("these tests tell no run of a decision")


@pytest.fixture
def sessions(engine) -> sessionmaker[Session]:
    return session_factory(engine.url.render_as_string(hide_password=False))


def new_scope(sessions: sessionmaker[Session]) -> RunScope:
    with transaction(sessions) as session:
        workspace = Workspace(name="Acme")
        session.add(workspace)
        session.flush()
        project = Project(
            workspace_id=workspace.id, name="App", source_repo_url="https://example.com/a.git"
        )
        session.add(project)
        session.flush()
        return RunScope(workspace_id=workspace.id, project_id=project.id)


@pytest.fixture
def scope(sessions) -> RunScope:
    return new_scope(sessions)


@pytest.fixture
def services(app, sessions, scope, repository_models, no_catalog, page_store, asset_store) -> None:
    """The application wired to the test database, this test's workspace and stores."""
    app.state.services = Services(
        sessions=sessions,
        workflows=NoWorkflows(),
        environment={},
        credential_cipher=None,
        model_router=repository_models,
        model_catalog=no_catalog,
        page_store=page_store,
        asset_store=asset_store,
    )
    app.dependency_overrides[current_workspace_id] = lambda: scope.workspace_id


def images_url(scope: RunScope, then: str = "") -> str:
    return f"/v1/projects/{scope.project_id}/repository-images{then}"


def kept_images(sessions, scope: RunScope, asset_store, count: int = 1) -> uuid.UUID:
    """An analysis of the project that is running and kept ``count`` images; its id."""
    project = {"workspace_id": scope.workspace_id, "project_id": scope.project_id}
    with transaction(sessions) as session:
        run = request_analysis_run(session, **project)
        mark_run_started(session, **project, run_id=run.id)
        images = []
        for n in range(count):
            sha256 = f"{n:064x}"
            key = repository_image_key(
                **project, analysis_run_id=run.id, sha256=sha256, mime="image/png"
            )
            asset_store.put(key, PNG, content_type="image/png")
            images.append(
                NewRepositoryImage(
                    path=f"assets/logo-{n}.png",
                    kind=RepositoryImageKind.LOGO,
                    mime="image/png",
                    width=512,
                    height=512,
                    size_bytes=len(PNG),
                    sha256=sha256,
                    storage_key=key,
                )
            )
        keep_repository_images(session, **project, analysis_run_id=run.id, images=images)
        return run.id


def test_the_list_holds_the_images_of_the_latest_analysis(
    client: TestClient, services, scope, sessions, asset_store
):
    run_id = kept_images(sessions, scope, asset_store, count=2)

    body = client.get(images_url(scope)).json()

    assert body["next_cursor"] is None
    assert sorted(image["path"] for image in body["images"]) == [
        "assets/logo-0.png",
        "assets/logo-1.png",
    ]
    first = body["images"][0]
    assert first["analysis_run_id"] == str(run_id)
    assert (first["kind"], first["mime"], first["width"], first["size_bytes"]) == (
        "logo",
        "image/png",
        512,
        len(PNG),
    )
    assert "storage_key" not in first and "sha256" not in first


def test_the_list_is_read_a_page_at_a_time(
    client: TestClient, services, scope, sessions, asset_store
):
    kept_images(sessions, scope, asset_store, count=3)

    first = client.get(images_url(scope), params={"limit": 2}).json()
    rest = client.get(images_url(scope), params={"limit": 2, "cursor": first["next_cursor"]})

    assert len(first["images"]) == 2
    assert (len(rest.json()["images"]), rest.json()["next_cursor"]) == (1, None)


def test_a_project_of_another_workspace_lists_nothing(
    client: TestClient, services, sessions, asset_store
):
    other = new_scope(sessions)
    kept_images(sessions, other, asset_store)

    response = client.get(images_url(other))

    assert (response.status_code, response.json()["code"]) == (404, "project_not_found")


def test_an_image_is_served_with_its_stored_type_and_safe_headers(
    client: TestClient, services, scope, sessions, asset_store
):
    kept_images(sessions, scope, asset_store)
    (listed,) = client.get(images_url(scope)).json()["images"]

    response = client.get(images_url(scope, f"/{listed['id']}/image"))

    assert (response.status_code, response.content) == (200, PNG)
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == f'inline; filename="{listed["id"]}.png"'
    assert response.headers["cache-control"].startswith("private, max-age=")


def test_an_image_of_another_project_is_not_served(
    client: TestClient, services, scope, sessions, asset_store
):
    other = new_scope(sessions)
    kept_images(sessions, other, asset_store)
    with transaction(sessions) as session:
        session.execute(
            update(Project)
            .where(Project.id == other.project_id)
            .values(workspace_id=scope.workspace_id)
        )
    (foreign,) = client.get(images_url(other)).json()["images"]

    response = client.get(images_url(scope, f"/{foreign['id']}/image"))

    assert (response.status_code, response.json()["code"]) == (404, "repository_image_not_found")


def test_an_unknown_image_is_404(client: TestClient, services, scope):
    response = client.get(images_url(scope, f"/{uuid.uuid4()}/image"))
    assert (response.status_code, response.json()["code"]) == (404, "repository_image_not_found")


def test_an_image_object_storage_cannot_give_back_is_503(
    client: TestClient, services, scope, sessions, asset_store
):
    kept_images(sessions, scope, asset_store)
    (listed,) = client.get(images_url(scope)).json()["images"]
    asset_store.unreachable = True

    response = client.get(images_url(scope, f"/{listed['id']}/image"))

    assert (response.status_code, response.json()["code"]) == (503, "storage_unavailable")
