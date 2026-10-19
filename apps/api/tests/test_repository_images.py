"""Tests for the routes of the product's images: an analysis's, and a person's curation of them.

They need PostgreSQL (see the root ``conftest.py``). Each test works in a workspace of its
own, committed, since a route commits its own transaction. Images are kept with the core
function the worker calls; their bytes live in the fake asset store.
"""

import base64
import io
import random
import uuid

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_api.dependencies import Services
from openmarketer_api.identity import current_workspace_id
from openmarketer_api.routes.repository_images import MAX_UPLOAD_BASE64_LENGTH
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
    assert (first["source"], first["kind_chosen"]) == ("analysis", False)
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


# ----------------------------------------------------------------- a person's curation
def jpeg_with_exif(width: int = 300, height: int = 200) -> bytes:
    image = Image.frombytes("RGB", (width, height), random.randbytes(width * height * 3))
    exif = Image.Exif()
    exif[0x010E] = "secret description"  # ImageDescription
    out = io.BytesIO()
    image.save(out, format="JPEG", exif=exif, quality=95)
    return out.getvalue()


def upload_body(data: bytes, *, name: str = "shot.jpg", kind: str = "screenshot") -> dict:
    return {"name": name, "kind": kind, "data_base64": base64.b64encode(data).decode("ascii")}


def listed_ids(client: TestClient, scope: RunScope) -> list[str]:
    return [image["id"] for image in client.get(images_url(scope)).json()["images"]]


def test_a_person_uploads_an_image_written_again_without_its_metadata(
    client: TestClient, services, scope, asset_store
):
    response = client.post(images_url(scope), json=upload_body(jpeg_with_exif()))

    assert response.status_code == 201
    body = response.json()
    assert (body["source"], body["analysis_run_id"], body["path"]) == ("person", None, "shot.jpg")
    assert (body["kind"], body["kind_chosen"], body["mime"]) == ("screenshot", True, "image/webp")
    assert listed_ids(client, scope) == [body["id"]]
    served = client.get(images_url(scope, f"/{body['id']}/image"))
    assert served.headers["content-type"] == "image/webp"
    assert b"secret description" not in served.content


def test_an_upload_that_is_not_an_image_is_400(client: TestClient, services, scope):
    response = client.post(images_url(scope), json=upload_body(b"just some text " * 300))
    assert (response.status_code, response.json()["code"]) == (400, "repository_image_refused")


def test_an_upload_that_is_not_base64_is_400(client: TestClient, services, scope):
    body = {"name": "a.png", "kind": "logo", "data_base64": "not base64!"}
    response = client.post(images_url(scope), json=body)
    assert (response.status_code, response.json()["code"]) == (400, "repository_image_refused")


def test_an_upload_over_the_limit_is_413(client: TestClient, services, scope):
    body = {"name": "a.png", "kind": "logo", "data_base64": "A" * (MAX_UPLOAD_BASE64_LENGTH + 4)}
    response = client.post(images_url(scope), json=body)
    assert (response.status_code, response.json()["code"]) == (413, "repository_image_too_large")


def test_an_upload_past_the_projects_limit_is_409(client: TestClient, services, scope, monkeypatch):
    monkeypatch.setattr(
        "openmarketer_core.repository_analysis.repository_images.curation.MAX_PERSON_IMAGES", 0
    )
    response = client.post(images_url(scope), json=upload_body(jpeg_with_exif()))
    assert (response.status_code, response.json()["code"]) == (409, "too_many_repository_images")


def test_an_upload_to_a_project_of_another_workspace_is_404(client: TestClient, services, sessions):
    other = new_scope(sessions)
    response = client.post(images_url(other), json=upload_body(jpeg_with_exif()))
    assert (response.status_code, response.json()["code"]) == (404, "project_not_found")


def test_a_person_chooses_an_images_kind(
    client: TestClient, services, scope, sessions, asset_store
):
    kept_images(sessions, scope, asset_store)
    (image_id,) = listed_ids(client, scope)

    response = client.patch(images_url(scope, f"/{image_id}"), json={"kind": "icon"})

    assert response.status_code == 200
    assert (response.json()["kind"], response.json()["kind_chosen"]) == ("icon", True)
    (listed,) = client.get(images_url(scope)).json()["images"]
    assert (listed["kind"], listed["kind_chosen"]) == ("icon", True)


def test_a_kind_outside_the_vocabulary_is_422(
    client: TestClient, services, scope, sessions, asset_store
):
    kept_images(sessions, scope, asset_store)
    (image_id,) = listed_ids(client, scope)
    response = client.patch(images_url(scope, f"/{image_id}"), json={"kind": "banner"})
    assert response.status_code == 422


def test_choosing_the_kind_of_an_unknown_image_is_404(client: TestClient, services, scope):
    response = client.patch(images_url(scope, f"/{uuid.uuid4()}"), json={"kind": "icon"})
    assert (response.status_code, response.json()["code"]) == (404, "repository_image_not_found")


def test_removing_an_analysis_image_keeps_its_file(
    client: TestClient, services, scope, sessions, asset_store
):
    kept_images(sessions, scope, asset_store)
    (image_id,) = listed_ids(client, scope)

    response = client.delete(images_url(scope, f"/{image_id}"))

    assert response.status_code == 204
    assert listed_ids(client, scope) == []
    assert asset_store.removed == []
    gone = client.get(images_url(scope, f"/{image_id}/image"))
    assert (gone.status_code, gone.json()["code"]) == (404, "repository_image_not_found")
    again = client.delete(images_url(scope, f"/{image_id}"))
    assert (again.status_code, again.json()["code"]) == (404, "repository_image_not_found")


def test_removing_an_upload_removes_its_file(client: TestClient, services, scope, asset_store):
    uploaded = client.post(images_url(scope), json=upload_body(jpeg_with_exif())).json()
    (key,) = asset_store.images

    response = client.delete(images_url(scope, f"/{uploaded['id']}"))

    assert response.status_code == 204
    assert (listed_ids(client, scope), asset_store.removed) == ([], [key])


def test_an_upload_whose_file_cannot_be_removed_stays(
    client: TestClient, services, scope, asset_store
):
    uploaded = client.post(images_url(scope), json=upload_body(jpeg_with_exif())).json()
    asset_store.unreachable = True

    response = client.delete(images_url(scope, f"/{uploaded['id']}"))

    assert (response.status_code, response.json()["code"]) == (503, "storage_unavailable")
    assert listed_ids(client, scope) == [uploaded["id"]]


def test_a_removal_with_a_body_is_refused(
    client: TestClient, services, scope, sessions, asset_store
):
    kept_images(sessions, scope, asset_store)
    (image_id,) = listed_ids(client, scope)
    response = client.request("DELETE", images_url(scope, f"/{image_id}"), json={"by": "me"})
    assert response.status_code == 422
    assert listed_ids(client, scope) == [image_id]
