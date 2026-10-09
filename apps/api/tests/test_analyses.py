"""Tests for the analysis routes: runs stored in PostgreSQL, workflows started on a fake.

They need PostgreSQL (see the root ``conftest.py``). A request to start an
analysis commits its own transactions, so the rows these tests set up are
committed too, each test in a workspace of its own. What the worker would
report is written with the same core functions the worker calls.
"""

import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_api.dependencies import Services
from openmarketer_api.identity import current_workspace_id
from openmarketer_core.db.analysis_runs import (
    mark_run_failed,
    mark_run_started,
    mark_run_succeeded,
)
from openmarketer_core.db.evidence_store import save_analysis_of_project
from openmarketer_core.db.models import Project, Workspace
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.profile import ProductProfile
from openmarketer_core.repository_analysis.intake import Snapshot
from openmarketer_core.repository_analysis.request import WORKFLOW_NOT_STARTED, WorkflowNotStarted
from openmarketer_core.repository_analysis.workflow_contract import AnalyzeRepositoryInput

PROFILE = ProductProfile.model_validate({"product": {"name": "Example App", "type": "dev_tool"}})


class FakeWorkflows:
    """Records the runs whose workflow was started; refuses while ``reachable`` is false."""

    def __init__(self) -> None:
        self.started: list[AnalyzeRepositoryInput] = []
        self.reachable = True

    async def start(self, run: AnalyzeRepositoryInput) -> None:
        if not self.reachable:
            raise WorkflowNotStarted("no workflow server in this test")
        self.started.append(run)


@pytest.fixture
def sessions(engine) -> sessionmaker[Session]:
    return session_factory(engine.url.render_as_string(hide_password=False))


def new_workspace(sessions: sessionmaker[Session]) -> uuid.UUID:
    with transaction(sessions) as session:
        workspace = Workspace(name="Acme")
        session.add(workspace)
        session.flush()
        return workspace.id


def new_project(
    sessions: sessionmaker[Session],
    workspace_id: uuid.UUID,
    repository_url: str = "https://example.com/acme/app.git",
) -> uuid.UUID:
    with transaction(sessions) as session:
        project = Project(workspace_id=workspace_id, name="App", source_repo_url=repository_url)
        session.add(project)
        session.flush()
        return project.id


@pytest.fixture
def workspace_id(sessions) -> uuid.UUID:
    return new_workspace(sessions)


@pytest.fixture
def workflows(app, sessions, workspace_id) -> FakeWorkflows:
    """The application wired to the test database, this test's workspace and fake workflows."""
    fake = FakeWorkflows()
    app.state.services = Services(sessions=sessions, analysis_workflows=fake)
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    return fake


@pytest.fixture
def project_id(sessions, workspace_id) -> uuid.UUID:
    return new_project(sessions, workspace_id)


def analyses_url(project_id: uuid.UUID) -> str:
    return f"/v1/projects/{project_id}/analyses"


@pytest.fixture
def run(client, workflows, sessions, workspace_id, project_id):
    """A started run, and the worker's reports about it."""
    return ReportedRun(
        sessions,
        AnalyzeRepositoryInput(
            workspace_id=workspace_id,
            project_id=project_id,
            run_id=uuid.UUID(client.post(analyses_url(project_id)).json()["id"]),
        ),
    )


class ReportedRun:
    def __init__(self, sessions: sessionmaker[Session], ids: AnalyzeRepositoryInput) -> None:
        self.sessions = sessions
        self.ids = ids
        self.url = f"{analyses_url(ids.project_id)}/{ids.run_id}"
        self.scope = {"workspace_id": ids.workspace_id, "project_id": ids.project_id}

    def picked_up(self) -> None:
        with transaction(self.sessions) as session:
            mark_run_started(session, run_id=self.ids.run_id, **self.scope)

    def failed(self, error: str) -> None:
        with transaction(self.sessions) as session:
            mark_run_failed(session, run_id=self.ids.run_id, error=error, **self.scope)

    def succeeded(self) -> None:
        snapshot = Snapshot(
            root=Path("/unused"), source_url="https://unused", commit_sha="a" * 40, ref="main"
        )
        with transaction(self.sessions) as session:
            saved = save_analysis_of_project(
                session, snapshot=snapshot, facts=[], profile=PROFILE, **self.scope
            )
            mark_run_succeeded(
                session, run_id=self.ids.run_id, profile_id=saved.profile_id, **self.scope
            )


# ------------------------------------------------------------------ start
def test_started_analysis_is_accepted_as_a_queued_run(client, workflows, project_id):
    response = client.post(analyses_url(project_id))
    assert response.status_code == 202
    body = response.json()
    assert set(body) == {
        "id",
        "project_id",
        "status",
        "created_at",
        "started_at",
        "finished_at",
        "error",
        "profile_version",
    }
    assert (body["project_id"], body["status"]) == (str(project_id), "queued")
    assert (body["started_at"], body["finished_at"], body["error"], body["profile_version"]) == (
        None,
        None,
        None,
        None,
    )


def test_started_analysis_is_stored_and_can_be_polled(client, workflows, project_id):
    started = client.post(analyses_url(project_id)).json()
    polled = client.get(f"{analyses_url(project_id)}/{started['id']}")
    assert (polled.status_code, polled.json()) == (200, started)


def test_workflow_is_started_for_the_stored_run(client, workflows, workspace_id, project_id):
    run_id = uuid.UUID(client.post(analyses_url(project_id)).json()["id"])
    assert workflows.started == [
        AnalyzeRepositoryInput(workspace_id=workspace_id, project_id=project_id, run_id=run_id)
    ]


def test_second_start_while_a_run_is_unfinished_is_a_conflict(client, workflows, project_id):
    client.post(analyses_url(project_id))
    response = client.post(analyses_url(project_id))
    assert (response.status_code, response.json()) == (
        409,
        {
            "code": "analysis_already_running",
            "message": f"project {project_id} already has an unfinished analysis run",
        },
    )
    assert len(workflows.started) == 1


def test_project_can_be_analysed_again_after_its_run_has_finished(client, workflows, run):
    run.failed("git clone failed")
    assert client.post(analyses_url(run.ids.project_id)).status_code == 202


def test_start_that_reaches_no_worker_is_unavailable_and_the_run_is_failed(
    client, workflows, sessions, project_id
):
    workflows.reachable = False
    response = client.post(analyses_url(project_id))
    assert response.status_code == 503
    assert response.json()["code"] == "analysis_not_started"
    run_id = response.json()["message"].split()[2]
    stored = client.get(f"{analyses_url(project_id)}/{run_id}").json()
    assert (stored["status"], stored["error"]) == ("failed", WORKFLOW_NOT_STARTED)
    assert stored["finished_at"] is not None


def test_start_that_reached_no_worker_does_not_block_the_next_one(client, workflows, project_id):
    workflows.reachable = False
    client.post(analyses_url(project_id))
    workflows.reachable = True
    assert client.post(analyses_url(project_id)).status_code == 202


def test_start_for_an_unknown_project_is_not_found(client, workflows):
    response = client.post(analyses_url(uuid.uuid4()))
    assert response.status_code == 404
    assert response.json()["code"] == "project_not_found"
    assert workflows.started == []


def test_start_for_a_project_of_another_workspace_is_not_found(client, workflows, sessions):
    foreign_project = new_project(sessions, new_workspace(sessions))
    assert client.post(analyses_url(foreign_project)).status_code == 404
    assert workflows.started == []


def test_project_of_a_local_folder_is_refused_and_gets_no_run(
    client, workflows, sessions, workspace_id
):
    """The command line stores such projects; a request must not make a worker read its disk."""
    local = new_project(sessions, workspace_id, "file:///etc")
    response = client.post(analyses_url(local))
    assert (response.status_code, response.json()) == (
        400,
        {"code": "invalid_repository_url", "message": "repository URL must start with https://"},
    )
    assert workflows.started == []


def test_start_takes_no_request_body(client, workflows, project_id):
    response = client.post(analyses_url(project_id), json={"repository_url": "file:///etc"})
    assert response.status_code == 422
    assert workflows.started == []


# ----------------------------------------------------------------- status
def test_status_of_a_run_a_worker_picked_up_is_running(client, run):
    run.picked_up()
    body = client.get(run.url).json()
    assert body["status"] == "running"
    assert body["started_at"] is not None
    assert body["finished_at"] is None


def test_status_of_a_succeeded_run_names_the_profile_version_it_stored(client, run):
    run.picked_up()
    run.succeeded()
    body = client.get(run.url).json()
    assert (body["status"], body["profile_version"], body["error"]) == ("succeeded", 1, None)
    assert body["finished_at"] is not None


def test_draft_of_a_succeeded_run_is_the_one_the_profile_route_returns(client, run):
    run.picked_up()
    run.succeeded()
    draft = client.get(f"/v1/projects/{run.ids.project_id}/profile/draft").json()
    assert draft["version"] == client.get(run.url).json()["profile_version"]
    assert ProductProfile.model_validate(draft["profile"]) == PROFILE


def test_status_of_a_failed_run_carries_the_reason_the_worker_recorded(client, run):
    run.picked_up()
    run.failed("git clone failed: Cloning into '<clone>/repo'... not found")
    body = client.get(run.url).json()
    assert (body["status"], body["error"], body["profile_version"]) == (
        "failed",
        "git clone failed: Cloning into '<clone>/repo'... not found",
        None,
    )


def test_unknown_run_is_not_found(client, workflows, project_id):
    run_id = uuid.uuid4()
    response = client.get(f"{analyses_url(project_id)}/{run_id}")
    assert (response.status_code, response.json()) == (
        404,
        {
            "code": "analysis_not_found",
            "message": f"project {project_id} has no analysis run {run_id}",
        },
    )


def test_run_is_not_found_under_another_project(client, run, sessions, workspace_id):
    other = new_project(sessions, workspace_id, "https://example.com/acme/other.git")
    assert client.get(f"{analyses_url(other)}/{run.ids.run_id}").status_code == 404


def test_run_is_not_found_from_another_workspace(app, client, run, sessions):
    elsewhere = new_workspace(sessions)
    app.dependency_overrides[current_workspace_id] = lambda: elsewhere
    assert client.get(run.url).status_code == 404


def test_malformed_run_identifier_is_rejected(client, workflows, project_id):
    assert client.get(f"{analyses_url(project_id)}/not-a-uuid").status_code == 422


# ------------------------------------------------------------------- list
def finished_run(client, sessions, workspace_id, project_id) -> dict:
    """Start a run, have the worker report its failure, and return it as the API shows it."""
    ids = AnalyzeRepositoryInput(
        workspace_id=workspace_id,
        project_id=project_id,
        run_id=uuid.UUID(client.post(analyses_url(project_id)).json()["id"]),
    )
    run = ReportedRun(sessions, ids)
    run.failed("git clone failed")
    return client.get(run.url).json()


def test_project_without_runs_lists_none(client, workflows, project_id):
    response = client.get(analyses_url(project_id))
    assert (response.status_code, response.json()) == (200, {"runs": [], "next_cursor": None})


def test_runs_are_listed_newest_first_as_they_are_now(
    client, workflows, sessions, workspace_id, project_id
):
    first = finished_run(client, sessions, workspace_id, project_id)
    second = finished_run(client, sessions, workspace_id, project_id)
    unfinished = client.post(analyses_url(project_id)).json()
    assert client.get(analyses_url(project_id)).json()["runs"] == [unfinished, second, first]


def test_listed_succeeded_run_names_the_profile_version_it_stored(client, run):
    run.picked_up()
    run.succeeded()
    listed = client.get(analyses_url(run.ids.project_id)).json()["runs"]
    assert listed == [client.get(run.url).json()]
    assert (listed[0]["status"], listed[0]["profile_version"]) == ("succeeded", 1)


def test_run_behind_a_conflict_is_the_first_one_listed(client, workflows, project_id):
    unfinished = client.post(analyses_url(project_id)).json()
    assert client.post(analyses_url(project_id)).status_code == 409
    assert client.get(analyses_url(project_id), params={"limit": 1}).json()["runs"] == [unfinished]


def test_list_holds_no_more_runs_than_the_limit_and_names_the_next_page(
    client, workflows, sessions, workspace_id, project_id
):
    runs = [finished_run(client, sessions, workspace_id, project_id) for _ in range(3)]
    page = client.get(analyses_url(project_id), params={"limit": 2}).json()
    assert page["runs"] == [runs[2], runs[1]]
    following = client.get(
        analyses_url(project_id), params={"limit": 2, "cursor": page["next_cursor"]}
    ).json()
    assert following == {"runs": [runs[0]], "next_cursor": None}


def test_run_started_between_two_pages_neither_repeats_nor_hides_one(
    client, workflows, sessions, workspace_id, project_id
):
    runs = [finished_run(client, sessions, workspace_id, project_id) for _ in range(4)]
    first = client.get(analyses_url(project_id), params={"limit": 2}).json()
    client.post(analyses_url(project_id))
    second = client.get(
        analyses_url(project_id), params={"limit": 2, "cursor": first["next_cursor"]}
    ).json()
    assert first["runs"] + second["runs"] == runs[::-1]


def test_runs_of_another_project_are_not_listed(client, run, sessions, workspace_id):
    other = new_project(sessions, workspace_id, "https://example.com/acme/other.git")
    assert client.get(analyses_url(other)).json()["runs"] == []


def test_runs_of_an_unknown_project_are_not_found(client, workflows):
    project_id = uuid.uuid4()
    response = client.get(analyses_url(project_id))
    assert (response.status_code, response.json()) == (
        404,
        {"code": "project_not_found", "message": f"project {project_id} not found"},
    )


def test_runs_of_a_project_of_another_workspace_are_answered_like_an_unknown_one(
    app, client, run, sessions
):
    foreign_workspace = new_workspace(sessions)
    app.dependency_overrides[current_workspace_id] = lambda: foreign_workspace
    response = client.get(analyses_url(run.ids.project_id))
    assert (response.status_code, response.json()) == (
        404,
        {"code": "project_not_found", "message": f"project {run.ids.project_id} not found"},
    )


@pytest.mark.parametrize("limit", [0, 101])
def test_limit_of_runs_outside_the_bounds_is_rejected(client, workflows, project_id, limit):
    assert client.get(analyses_url(project_id), params={"limit": limit}).status_code == 422
