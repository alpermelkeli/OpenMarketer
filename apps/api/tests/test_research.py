"""Tests for the research routes: a real database and a recording page store.

They need PostgreSQL (see the root ``conftest.py``). Each test works in a workspace of its
own, committed, since a route commits its own transaction; the page store is a fake that
remembers the keys it removed. Deleting a run is the only route covered here so far.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_api.dependencies import Services
from openmarketer_api.identity import current_workspace_id
from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.db.models import Project, ResearchRunStatus, Workspace
from openmarketer_core.db.research_pages import NewPage, store_page
from openmarketer_core.db.research_runs import (
    mark_run_started,
    mark_run_succeeded,
    request_research_run,
)
from openmarketer_core.db.research_store import save_research_report
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.research.research_agent.rules import Report

REQUEST = ResearchRequest(questions=["What did competitors post last month?"])


class NoWorkflows:
    """The workflow engine of these tests: nothing asks it for anything."""

    async def start(self, workflow) -> None:
        raise AssertionError("these tests start no workflow")

    async def tell_decided(self, workflow_id: str, proposal_id: uuid.UUID) -> None:
        raise AssertionError("these tests tell no run of a decision")


@pytest.fixture
def sessions(engine) -> sessionmaker[Session]:
    return session_factory(engine.url.render_as_string(hide_password=False))


@pytest.fixture
def scope(sessions) -> RunScope:
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
def services(app, sessions, scope, repository_models, no_catalog, page_store) -> None:
    """The application wired to the test database, this test's workspace and page store."""
    app.state.services = Services(
        sessions=sessions,
        workflows=NoWorkflows(),
        environment={},
        credential_cipher=None,
        model_router=repository_models,
        model_catalog=no_catalog,
        page_store=page_store,
    )
    app.dependency_overrides[current_workspace_id] = lambda: scope.workspace_id


def runs_url(scope: RunScope) -> str:
    return f"/v1/projects/{scope.project_id}/researches"


def run_url(scope: RunScope, run_id: object) -> str:
    return f"{runs_url(scope)}/{run_id}"


def store_page_rows(
    session: Session, scope: RunScope, run_id: uuid.UUID, keys: tuple[str, ...]
) -> None:
    for key in keys:
        store_page(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            run_id=run_id,
            page_id=uuid.uuid4(),
            page=NewPage(
                url=f"https://example.com/{key}",
                title="A page",
                content_type="text/html",
                size_bytes=10,
                storage_key=key,
                sha256="a" * 64,
                flags=(),
            ),
        )


def store_finished_run(
    sessions: sessionmaker[Session], scope: RunScope, *, keys: tuple[str, ...]
) -> uuid.UUID:
    """A finished run of the workspace, with a report and one page per key."""
    with transaction(sessions) as session:
        run = request_research_run(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            request=REQUEST,
            requested_by="person",
        )
        mark_run_started(
            session, workspace_id=scope.workspace_id, project_id=scope.project_id, run_id=run.id
        )
        store_page_rows(session, scope, run.id, keys)
        report = save_research_report(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            run_id=run.id,
            report=Report(
                questions=list(REQUEST.questions),
                findings=[],
                notes=[],
                cost_usd=0.01,
                model="fake/model",
            ),
        )
        mark_run_succeeded(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            run_id=run.id,
            report_id=report.id,
        )
        return run.id


def store_queued_run(sessions: sessionmaker[Session], scope: RunScope) -> uuid.UUID:
    """A run the worker has not picked up yet."""
    with transaction(sessions) as session:
        return request_research_run(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            request=REQUEST,
            requested_by="person",
        ).id


def test_deleting_a_finished_run_removes_it_with_its_pages(
    client: TestClient, services, scope, sessions, page_store
):
    run_id = store_finished_run(sessions, scope, keys=("pages/a.html", "pages/b.html"))

    response = client.delete(run_url(scope, run_id))

    assert (response.status_code, response.content) == (204, b"")
    assert sorted(page_store.removed) == ["pages/a.html", "pages/b.html"]
    assert client.get(run_url(scope, run_id)).status_code == 404
    assert client.get(run_url(scope, run_id) + "/report").status_code == 404


def test_a_queued_run_is_not_deleted(client: TestClient, services, scope, sessions, page_store):
    run_id = store_queued_run(sessions, scope)

    response = client.delete(run_url(scope, run_id))

    assert (response.status_code, response.json()) == (
        409,
        {
            "code": "research_unfinished",
            "message": f"research run {run_id} is still queued;"
            " it can be deleted when it has finished",
        },
    )
    assert client.get(run_url(scope, run_id)).json()["status"] == ResearchRunStatus.QUEUED
    assert page_store.removed == []


def test_a_run_stays_when_its_pages_cannot_be_removed(
    client: TestClient, services, scope, sessions, page_store
):
    run_id = store_finished_run(sessions, scope, keys=("pages/a.html",))
    page_store.unreachable = True

    response = client.delete(run_url(scope, run_id))

    assert (response.status_code, response.json()) == (
        503,
        {
            "code": "storage_unavailable",
            "message": "object storage is not reachable",
        },
    )
    assert client.get(run_url(scope, run_id)).status_code == 200
    assert page_store.removed == []


def test_deleting_an_unknown_run_is_not_found(client: TestClient, services, scope):
    missing = uuid.uuid4()
    response = client.delete(run_url(scope, missing))
    assert (response.status_code, response.json()) == (
        404,
        {
            "code": "research_not_found",
            "message": f"project {scope.project_id} has no research run {missing}",
        },
    )


def test_deleting_a_run_takes_no_body(client: TestClient, services, scope, sessions):
    run_id = store_finished_run(sessions, scope, keys=())
    response = client.request("DELETE", run_url(scope, run_id), json={"force": True})
    assert response.status_code == 422
    assert client.get(run_url(scope, run_id)).status_code == 200
