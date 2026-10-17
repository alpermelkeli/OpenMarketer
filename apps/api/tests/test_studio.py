"""Tests for the studio routes: a real database, a recording workflow starter and image store.

They need PostgreSQL (see the root ``conftest.py``). Each test works in a workspace of its
own, committed, since a route commits its own transaction. Runs, assets and versions are
stored with the core functions the worker calls; images live in the fake asset store.
"""

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_api.dependencies import Services
from openmarketer_api.identity import LOCAL_USER_ID, current_workspace_id
from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.db.media_assets import NewAssetVersion, add_version
from openmarketer_core.db.models import (
    AssetTechnique,
    Project,
    StudioRunRecord,
    StudioRunStatus,
    Workspace,
)
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.db.studio_runs import (
    mark_run_started,
    mark_run_succeeded,
    request_studio_run,
)
from openmarketer_core.studio.asset_store import asset_key
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.studio.workflow_contract import (
    STUDIO_TASK_QUEUE,
    STUDIO_WORKFLOW,
    StudioRunInput,
)
from openmarketer_core.workflow_start import WorkflowNotStarted, WorkflowStart

REQUEST = AssetRequest(goal="Announce v2", exact_copy=["v2 is out"], platform=Platform.X_POST)
PNG = b"\x89PNG\r\n\x1a\n made-up image bytes"


class RecordingWorkflows:
    """The workflow engine of these tests: what it was asked to start, unless unreachable."""

    def __init__(self) -> None:
        self.started: list[WorkflowStart] = []
        self.reachable = True

    async def start(self, workflow: WorkflowStart) -> None:
        if not self.reachable:
            raise WorkflowNotStarted("no workflow server in this test")
        self.started.append(workflow)

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
def workflows() -> RecordingWorkflows:
    return RecordingWorkflows()


@pytest.fixture
def services(
    app, sessions, scope, workflows, repository_models, no_catalog, page_store, asset_store
) -> None:
    """The application wired to the test database, this test's workspace and stores."""
    app.state.services = Services(
        sessions=sessions,
        workflows=workflows,
        environment={},
        credential_cipher=None,
        model_router=repository_models,
        model_catalog=no_catalog,
        page_store=page_store,
        asset_store=asset_store,
    )
    app.dependency_overrides[current_workspace_id] = lambda: scope.workspace_id


def runs_url(scope: RunScope, then: str = "") -> str:
    return f"/v1/projects/{scope.project_id}/studio-runs{then}"


def assets_url(scope: RunScope, then: str = "") -> str:
    return f"/v1/projects/{scope.project_id}/media-assets{then}"


def queued_run(sessions: sessionmaker[Session], scope: RunScope) -> uuid.UUID:
    with transaction(sessions) as session:
        return request_studio_run(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            request=REQUEST,
            requested_by="person",
        ).id


def stored_version(
    session: Session, scope: RunScope, run_id: uuid.UUID, asset_store, *, round: int = 1
):
    version_id = uuid.uuid4()
    key = asset_key(
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        run_id=run_id,
        version_id=version_id,
        mime="image/png",
    )
    asset_store.images[key] = PNG
    return add_version(
        session,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        run_id=run_id,
        version_id=version_id,
        action_id=f"studio-run:{run_id}:{round}",
        version=NewAssetVersion(
            round=round,
            technique=AssetTechnique.COMPOSED,
            storage_key=key,
            mime="image/png",
            width=1600,
            height=900,
            size_bytes=len(PNG),
            cost_usd=Decimal("0.04"),
            provider="sandbox",
            prompt="a launch card",
            params={"seed": 7},
            code="print('a card')",
        ),
    )


def finished_run(sessions: sessionmaker[Session], scope: RunScope, asset_store, *, versions=1):
    """A run that succeeded with one asset of ``versions`` versions; answers the run and asset."""
    run_id = queued_run(sessions, scope)
    with transaction(sessions) as session:
        mark_run_started(
            session, workspace_id=scope.workspace_id, project_id=scope.project_id, run_id=run_id
        )
        first = stored_version(session, scope, run_id, asset_store)
        last = first
        for round in range(2, versions + 1):
            version_id = uuid.uuid4()
            key = asset_key(
                workspace_id=scope.workspace_id,
                project_id=scope.project_id,
                run_id=run_id,
                version_id=version_id,
                mime="image/png",
            )
            asset_store.images[key] = PNG
            last = add_version(
                session,
                workspace_id=scope.workspace_id,
                project_id=scope.project_id,
                run_id=run_id,
                version_id=version_id,
                action_id=f"studio-run:{run_id}:{round}",
                asset_id=first.asset_id,
                version=NewAssetVersion(
                    round=round,
                    technique=AssetTechnique.GENERATED,
                    storage_key=key,
                    mime="image/png",
                    width=1600,
                    height=900,
                    size_bytes=len(PNG),
                    cost_usd=Decimal("0.04"),
                ),
            )
        mark_run_succeeded(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            run_id=run_id,
            final_version_id=last.id,
            cost_usd=Decimal("0.08"),
        )
    return run_id, first.asset_id, last


def running_run_with_asset(sessions, scope, asset_store):
    run_id = queued_run(sessions, scope)
    with transaction(sessions) as session:
        mark_run_started(
            session, workspace_id=scope.workspace_id, project_id=scope.project_id, run_id=run_id
        )
        version = stored_version(session, scope, run_id, asset_store)
    return run_id, version


def test_start_answers_202_and_starts_the_workflow_with_identifiers_only(
    client: TestClient, services, scope, workflows
):
    response = client.post(runs_url(scope), json=REQUEST.model_dump(mode="json"))

    assert response.status_code == 202
    body = response.json()
    assert (body["status"], body["platform"], body["requested_by"]) == (
        "queued",
        "x_post",
        "person",
    )
    [started] = workflows.started
    run_id = uuid.UUID(body["id"])
    assert (started.workflow, started.task_queue, started.workflow_id) == (
        STUDIO_WORKFLOW,
        STUDIO_TASK_QUEUE,
        f"studio-{run_id}",
    )
    assert started.input == StudioRunInput(
        workspace_id=scope.workspace_id, project_id=scope.project_id, run_id=run_id
    )


def test_a_run_that_cannot_be_started_is_failed_and_answered_503(
    client: TestClient, services, scope, workflows
):
    workflows.reachable = False

    response = client.post(runs_url(scope), json=REQUEST.model_dump(mode="json"))

    assert (response.status_code, response.json()["code"]) == (503, "studio_not_started")
    [run] = client.get(runs_url(scope)).json()["runs"]
    assert run["status"] == StudioRunStatus.FAILED


def test_start_refuses_unknown_fields(client: TestClient, services, scope, workflows):
    body = {**REQUEST.model_dump(mode="json"), "approved": True}
    assert client.post(runs_url(scope), json=body).status_code == 422
    assert workflows.started == []


def test_a_reference_asset_of_another_project_is_refused_beside_its_field(
    client: TestClient, services, scope, sessions, asset_store, workflows
):
    other = new_scope(sessions)
    _, foreign_asset, _ = finished_run(sessions, other, asset_store)
    body = {**REQUEST.model_dump(mode="json"), "reference_asset_ids": [str(foreign_asset)]}

    response = client.post(runs_url(scope), json=body)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "reference_asset_ids"]
    assert workflows.started == []


def test_start_for_a_project_of_another_workspace_is_not_found(
    client: TestClient, services, sessions, workflows
):
    other = new_scope(sessions)
    response = client.post(runs_url(other), json=REQUEST.model_dump(mode="json"))
    assert (response.status_code, response.json()["code"]) == (404, "project_not_found")
    assert workflows.started == []


def test_list_and_get_show_a_run_as_it_is(
    client: TestClient, services, scope, sessions, asset_store
):
    run_id, asset_id, _ = finished_run(sessions, scope, asset_store)

    [listed] = client.get(runs_url(scope)).json()["runs"]
    one = client.get(runs_url(scope, f"/{run_id}")).json()

    assert listed == {key: value for key, value in one.items() if key != "request"}
    assert (one["status"], one["final_asset_id"], one["cost_usd"]) == (
        "succeeded",
        str(asset_id),
        0.08,
    )


def test_a_run_is_read_with_what_it_was_asked_for(client: TestClient, services, scope, sessions):
    run_id = queued_run(sessions, scope)

    request = client.get(runs_url(scope, f"/{run_id}")).json()["request"]

    assert request == {
        "goal": "Announce v2",
        "exact_copy": ["v2 is out"],
        "platform": "x_post",
        "style_notes": "",
        "reference_asset_ids": [],
    }


def test_a_stored_request_that_no_longer_fits_is_answered_as_none(
    client: TestClient, services, scope, sessions
):
    run_id = queued_run(sessions, scope)
    with transaction(sessions) as session:
        session.execute(
            update(StudioRunRecord)
            .where(StudioRunRecord.id == run_id)
            .values(request={"goal": "Announce v2", "unknown": True})
        )

    response = client.get(runs_url(scope, f"/{run_id}"))

    assert (response.status_code, response.json()["request"]) == (200, None)


def test_run_list_is_paged(client: TestClient, services, scope, sessions):
    newer, older = queued_run(sessions, scope), queued_run(sessions, scope)
    first = client.get(runs_url(scope), params={"limit": 1}).json()
    second = client.get(runs_url(scope), params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert {first["runs"][0]["id"], second["runs"][0]["id"]} == {str(newer), str(older)}
    assert second["next_cursor"] is None


def test_a_cursor_the_api_did_not_write_is_refused(client: TestClient, services, scope):
    assert client.get(runs_url(scope), params={"cursor": "made-up"}).status_code == 422


def test_an_unknown_run_is_not_found(client: TestClient, services, scope):
    response = client.get(runs_url(scope, f"/{uuid.uuid4()}"))
    assert (response.status_code, response.json()["code"]) == (404, "studio_run_not_found")


def test_deleting_a_finished_run_removes_it_with_its_images(
    client: TestClient, services, scope, sessions, asset_store
):
    run_id, asset_id, last = finished_run(sessions, scope, asset_store, versions=2)

    response = client.delete(runs_url(scope, f"/{run_id}"))

    assert (response.status_code, response.content) == (204, b"")
    assert sorted(asset_store.removed) == sorted(asset_store.images)
    assert client.get(runs_url(scope, f"/{run_id}")).status_code == 404
    assert client.get(assets_url(scope, f"/{asset_id}")).status_code == 404


def test_a_running_run_is_not_deleted(client: TestClient, services, scope, sessions, asset_store):
    run_id, _ = running_run_with_asset(sessions, scope, asset_store)

    response = client.delete(runs_url(scope, f"/{run_id}"))

    assert (response.status_code, response.json()["code"]) == (409, "studio_run_unfinished")
    assert asset_store.removed == []


def test_a_run_stays_when_its_images_cannot_be_removed(
    client: TestClient, services, scope, sessions, asset_store
):
    run_id, _, _ = finished_run(sessions, scope, asset_store)
    asset_store.unreachable = True

    response = client.delete(runs_url(scope, f"/{run_id}"))

    assert (response.status_code, response.json()["code"]) == (503, "storage_unavailable")
    assert client.get(runs_url(scope, f"/{run_id}")).status_code == 200


def test_deleting_a_run_takes_no_body(client: TestClient, services, scope, sessions, asset_store):
    run_id, _, _ = finished_run(sessions, scope, asset_store)
    response = client.request("DELETE", runs_url(scope, f"/{run_id}"), json={"force": True})
    assert response.status_code == 422
    assert client.get(runs_url(scope, f"/{run_id}")).status_code == 200


def test_the_library_lists_the_projects_assets(
    client: TestClient, services, scope, sessions, asset_store
):
    run_id, asset_id, last = finished_run(sessions, scope, asset_store)

    body = client.get(assets_url(scope)).json()

    assert body["next_cursor"] is None
    [asset] = body["assets"]
    assert (asset["id"], asset["run_id"], asset["status"]) == (str(asset_id), str(run_id), "draft")
    assert asset["current_version_id"] == str(last.id)


def test_an_asset_is_read_with_its_versions_and_without_their_code(
    client: TestClient, services, scope, sessions, asset_store
):
    _, asset_id, last = finished_run(sessions, scope, asset_store, versions=2)

    body = client.get(assets_url(scope, f"/{asset_id}")).json()

    assert body["asset"]["id"] == str(asset_id)
    # Stored in one transaction, the two share a creation time; which is listed first is not
    # this test's concern.
    versions = {version["round"]: version for version in body["versions"]}
    first, newest = versions[1], versions[2]
    assert newest["id"] == str(last.id)
    assert (first["technique"], first["provider"], first["prompt"]) == (
        "composed",
        "sandbox",
        "a launch card",
    )
    assert (first["params"], first["cost_usd"], first["passed"]) == ({"seed": 7}, 0.04, None)
    assert (first["has_code"], first["code_chars"]) == (True, len("print('a card')"))
    assert "code" not in first
    assert (newest["has_code"], newest["code_chars"]) == (False, 0)


def test_an_asset_of_another_workspace_is_not_found(
    client: TestClient, services, sessions, asset_store
):
    other = new_scope(sessions)
    _, asset_id, _ = finished_run(sessions, other, asset_store)
    response = client.get(assets_url(other, f"/{asset_id}"))
    assert (response.status_code, response.json()["code"]) == (404, "media_asset_not_found")


def test_accepting_records_the_current_user(
    client: TestClient, services, scope, sessions, asset_store
):
    _, asset_id, _ = finished_run(sessions, scope, asset_store)

    response = client.post(assets_url(scope, f"/{asset_id}/accept"))

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["decided_by"]) == ("accepted", str(LOCAL_USER_ID))
    assert body["decided_at"] is not None


def test_accepting_again_changes_nothing(
    client: TestClient, services, scope, sessions, asset_store
):
    _, asset_id, _ = finished_run(sessions, scope, asset_store)
    first = client.post(assets_url(scope, f"/{asset_id}/accept")).json()

    again = client.post(assets_url(scope, f"/{asset_id}/accept"))

    assert (again.status_code, again.json()) == (200, first)


def test_an_accepted_asset_cannot_be_rejected(
    client: TestClient, services, scope, sessions, asset_store
):
    _, asset_id, _ = finished_run(sessions, scope, asset_store)
    client.post(assets_url(scope, f"/{asset_id}/accept"))

    response = client.post(assets_url(scope, f"/{asset_id}/reject"))

    assert (response.status_code, response.json()["code"]) == (409, "media_asset_decided")


def test_a_rejected_asset_cannot_be_accepted(
    client: TestClient, services, scope, sessions, asset_store
):
    _, asset_id, _ = finished_run(sessions, scope, asset_store)
    assert client.post(assets_url(scope, f"/{asset_id}/reject")).json()["status"] == "rejected"

    response = client.post(assets_url(scope, f"/{asset_id}/accept"))

    assert (response.status_code, response.json()["code"]) == (409, "media_asset_decided")


def test_an_asset_of_a_running_run_is_not_decided(
    client: TestClient, services, scope, sessions, asset_store
):
    _, version = running_run_with_asset(sessions, scope, asset_store)
    response = client.post(assets_url(scope, f"/{version.asset_id}/accept"))
    assert (response.status_code, response.json()["code"]) == (409, "studio_run_unfinished")


def test_a_decision_takes_no_body(client: TestClient, services, scope, sessions, asset_store):
    _, asset_id, _ = finished_run(sessions, scope, asset_store)
    response = client.post(
        assets_url(scope, f"/{asset_id}/accept"), json={"decided_by": str(uuid.uuid4())}
    )
    assert response.status_code == 422
    assert client.get(assets_url(scope, f"/{asset_id}")).json()["asset"]["status"] == "draft"


def test_the_image_is_served_with_its_stored_type_and_safe_headers(
    client: TestClient, services, scope, sessions, asset_store
):
    _, asset_id, last = finished_run(sessions, scope, asset_store)

    response = client.get(assets_url(scope, f"/{asset_id}/versions/{last.id}/image"))

    assert (response.status_code, response.content) == (200, PNG)
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == f'inline; filename="{last.id}.png"'
    assert response.headers["cache-control"].startswith("private, max-age=")


def test_the_download_is_an_attachment(client: TestClient, services, scope, sessions, asset_store):
    _, asset_id, last = finished_run(sessions, scope, asset_store)

    response = client.get(assets_url(scope, f"/{asset_id}/versions/{last.id}/download"))

    assert (response.status_code, response.content) == (200, PNG)
    assert response.headers["content-disposition"] == f'attachment; filename="{last.id}.png"'


def test_a_version_of_another_asset_is_not_served(
    client: TestClient, services, scope, sessions, asset_store
):
    _, asset_id, _ = finished_run(sessions, scope, asset_store)
    _, _, other_version = finished_run(sessions, scope, asset_store)

    response = client.get(assets_url(scope, f"/{asset_id}/versions/{other_version.id}/image"))

    assert (response.status_code, response.json()["code"]) == (
        404,
        "media_asset_version_not_found",
    )


def test_a_version_of_another_project_is_not_served(
    client: TestClient, services, scope, sessions, asset_store
):
    other = new_scope(sessions)
    _, asset_id, version = finished_run(sessions, other, asset_store)

    # The other project's ids, asked of under this workspace's own project.
    response = client.get(assets_url(scope, f"/{asset_id}/versions/{version.id}/image"))

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"


def test_an_image_object_storage_cannot_give_back_is_503(
    client: TestClient, services, scope, sessions, asset_store
):
    _, asset_id, last = finished_run(sessions, scope, asset_store)
    asset_store.unreachable = True

    response = client.get(assets_url(scope, f"/{asset_id}/versions/{last.id}/image"))

    assert (response.status_code, response.json()["code"]) == (503, "storage_unavailable")
