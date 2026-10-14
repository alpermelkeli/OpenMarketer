"""Tests for the proposal routes: proposals stored in PostgreSQL, runs told through a fake.

They need PostgreSQL (see the root ``conftest.py``). A decision commits its own transaction, so
the rows these tests set up are committed too, each test in a workspace of its own. A proposal
is stored with the core function the worker calls.
"""

import uuid

import pytest
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_api.dependencies import Services
from openmarketer_api.identity import LOCAL_USER_ID, current_workspace_id
from openmarketer_core.agent_runtime.actions import ActionEffect, ActionRequest
from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.approvals.decision import RunNotTold
from openmarketer_core.db.models import Project, Workspace
from openmarketer_core.db.proposals import ActionProposal, propose_action
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.workflow_start import WorkflowStart


class FakeSignals:
    """The workflow engine of these tests: records the runs it was asked to tell, refuses while
    ``reachable`` is false, and starts nothing."""

    def __init__(self) -> None:
        self.told: list[tuple[str, uuid.UUID]] = []
        self.reachable = True

    async def start(self, workflow: WorkflowStart) -> None:
        raise AssertionError("deciding on a proposal starts no workflow")

    async def tell_decided(self, workflow_id: str, proposal_id: uuid.UUID) -> None:
        if not self.reachable:
            raise RunNotTold("no workflow server in this test")
        self.told.append((workflow_id, proposal_id))


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
def signals(app, sessions, scope, repository_models, no_catalog) -> FakeSignals:
    """The application wired to the test database, this test's workspace and fake signals."""
    fake = FakeSignals()
    app.state.services = Services(
        sessions=sessions,
        workflows=fake,
        environment={},
        credential_cipher=None,
        model_router=repository_models,
        model_catalog=no_catalog,
    )
    app.dependency_overrides[current_workspace_id] = lambda: scope.workspace_id
    return fake


def proposed(sessions: sessionmaker[Session], scope: RunScope, text: str = "v2 is out"):
    run = GraphRunRef(kind="writer-run", run_id=uuid.uuid4())
    with transaction(sessions) as session:
        return propose_action(
            session,
            scope=scope,
            run=run,
            workflow_id=f"write-{run.run_id}",
            request=ActionRequest(f"writer-run:{run.run_id}:1", "publish_post", {"text": text}),
            effect=ActionEffect.PUBLISHES,
            wait_seconds=3600,
        )


def url(scope: RunScope, proposal: ActionProposal | None = None, then: str = "") -> str:
    base = f"/v1/projects/{scope.project_id}/proposals"
    return base if proposal is None else f"{base}/{proposal.id}{then}"


def test_list_holds_the_projects_proposals_with_what_each_asks_for(
    client, signals, sessions, scope
):
    proposal = proposed(sessions, scope)

    body = client.get(url(scope)).json()

    assert body["next_cursor"] is None
    [listed] = body["proposals"]
    assert listed["id"] == str(proposal.id)
    assert (listed["action_name"], listed["effect"]) == ("publish_post", "publishes")
    assert listed["payload"] == {"text": "v2 is out"}
    assert (listed["status"], listed["decided_by"]) == ("pending", None)


def test_list_can_hold_the_pending_proposals_only(client, signals, sessions, scope):
    decided = proposed(sessions, scope)
    pending = proposed(sessions, scope)
    client.post(url(scope, decided, "/rejection"))

    body = client.get(url(scope), params={"status": "pending"}).json()

    assert [p["id"] for p in body["proposals"]] == [str(pending.id)]


def test_one_proposal_is_read_by_its_id(client, signals, sessions, scope):
    proposal = proposed(sessions, scope)

    response = client.get(url(scope, proposal))

    assert (response.status_code, response.json()["id"]) == (200, str(proposal.id))


def test_approval_is_recorded_as_the_current_user_and_the_run_is_told(
    client, signals, sessions, scope
):
    proposal = proposed(sessions, scope)

    response = client.post(url(scope, proposal, "/approval"))

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert response.json()["decided_by"] == str(LOCAL_USER_ID)
    assert signals.told == [(proposal.workflow_id, proposal.id)]


def test_rejection_is_recorded_and_the_run_is_told(client, signals, sessions, scope):
    proposal = proposed(sessions, scope)

    response = client.post(url(scope, proposal, "/rejection"))

    assert response.json()["status"] == "rejected"
    assert signals.told == [(proposal.workflow_id, proposal.id)]


def test_decided_proposal_is_not_decided_again(client, signals, sessions, scope):
    proposal = proposed(sessions, scope)
    client.post(url(scope, proposal, "/rejection"))

    response = client.post(url(scope, proposal, "/approval"))

    assert response.status_code == 409
    assert response.json()["code"] == "proposal_not_pending"
    assert client.get(url(scope, proposal)).json()["status"] == "rejected"


def test_decision_stands_when_the_run_cannot_be_told(client, signals, sessions, scope):
    signals.reachable = False
    proposal = proposed(sessions, scope)

    response = client.post(url(scope, proposal, "/approval"))

    assert response.json()["status"] == "approved"
    assert client.get(url(scope, proposal)).json()["status"] == "approved"


def test_approval_takes_no_body_so_no_approver_can_be_named(client, signals, sessions, scope):
    proposal = proposed(sessions, scope)

    response = client.post(
        url(scope, proposal, "/approval"), json={"decided_by": str(uuid.uuid4())}
    )

    assert response.status_code == 422
    assert client.get(url(scope, proposal)).json()["status"] == "pending"


def test_proposal_that_does_not_exist_is_not_found(client, signals, scope):
    missing = f"{url(scope)}/{uuid.uuid4()}"

    for response in (client.get(missing), client.post(f"{missing}/approval")):
        assert (response.status_code, response.json()["code"]) == (404, "proposal_not_found")


def test_proposal_of_another_workspace_is_not_found(client, signals, sessions, scope):
    with transaction(sessions) as session:
        other = Workspace(name="Other")
        session.add(other)
        session.flush()
        project = Project(
            workspace_id=other.id, name="Theirs", source_repo_url="https://example.com/t.git"
        )
        session.add(project)
        session.flush()
        theirs = RunScope(workspace_id=other.id, project_id=project.id)
    proposal = proposed(sessions, theirs)

    response = client.post(url(theirs, proposal, "/approval"))

    assert response.status_code == 404
    assert signals.told == []
