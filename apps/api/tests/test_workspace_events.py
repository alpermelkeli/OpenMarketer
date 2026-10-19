"""Tests for the workspace event stream: every run event of every project, from a cursor.

They need PostgreSQL (see the root ``conftest.py``) and reach no network: the model the stream
never resolves is the repository's configuration itself. The stream's own timing is compressed
by overriding the dependency, and events are stored with the core functions the worker calls.
"""

import json
import uuid

import pytest
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_api.dependencies import Services, event_stream_timing
from openmarketer_api.event_stream import StreamTiming
from openmarketer_api.identity import current_workspace_id
from openmarketer_core.agent_runtime.actions import ActionEffect, ActionRequest
from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.db.models import Project, Workspace
from openmarketer_core.db.proposals import propose_action
from openmarketer_core.db.run_events import append_events
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.run_events import TOOL_CALLED, RunEvent
from openmarketer_core.workflow_start import WorkflowStart

STREAM_URL = "/v1/events/stream"


class NoWorkflows:
    """The workflow engine of these tests: nothing is started and no run is told."""

    async def start(self, workflow: WorkflowStart) -> None:
        raise AssertionError("an event stream starts no workflow")

    async def tell_decided(self, workflow_id: str, proposal_id: uuid.UUID) -> None:
        raise AssertionError("an event stream tells no run")


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
def services(app, sessions, scope, repository_models, no_catalog, page_store, asset_store) -> None:
    """The application wired to the test database and this test's workspace."""
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


def quick_streams(app, *, close_after: float = 0.2) -> None:
    app.dependency_overrides[event_stream_timing] = lambda: StreamTiming(
        poll_every=0.01,
        keep_alive_every=60,
        close_after=close_after,
        reconnect_after=0.5,
    )


def blocks(body: str) -> list[str]:
    return [block for block in body.split("\n\n") if block]


def streamed_events(body: str) -> list[dict]:
    """The events of a stream's body: each one's fields, with its data read as JSON."""
    found = []
    for block in blocks(body):
        fields = dict(line.split(": ", 1) for line in block.split("\n") if not line.startswith(":"))
        if "data" in fields:
            found.append({**fields, "data": json.loads(fields["data"])})
    return found


def record(sessions: sessionmaker[Session], scope: RunScope, *labels: str) -> list[int]:
    """Store a `tool_called` event of one run per label, and return their identifiers."""
    with transaction(sessions) as session:
        stored = append_events(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            run_kind="orchestrator-chat",
            run_id=uuid.uuid4(),
            events=[RunEvent(TOOL_CALLED, {"tool": label}) for label in labels],
        )
        return [event.id for event in stored]


def second_project(sessions: sessionmaker[Session], scope: RunScope) -> RunScope:
    with transaction(sessions) as session:
        project = Project(
            workspace_id=scope.workspace_id,
            name="Other",
            source_repo_url="https://example.com/o.git",
        )
        session.add(project)
        session.flush()
        return RunScope(workspace_id=scope.workspace_id, project_id=project.id)


def test_the_stream_sends_every_projects_events_oldest_first(
    client, services, sessions, scope, app
):
    quick_streams(app)
    elsewhere = second_project(sessions, scope)
    ids = record(sessions, scope, "status", "profile")
    ids += record(sessions, elsewhere, "runs")
    with transaction(sessions) as session:
        other = Workspace(name="Other")
        session.add(other)
        session.flush()
        project = Project(
            workspace_id=other.id, name="Theirs", source_repo_url="https://example.com/t.git"
        )
        session.add(project)
        session.flush()
    record(sessions, RunScope(workspace_id=other.id, project_id=project.id), "theirs")

    response = client.get(STREAM_URL, params={"after": 0})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert blocks(response.text)[0] == "retry: 500"
    sent = streamed_events(response.text)
    assert [int(event["id"]) for event in sent] == ids
    assert [event["data"]["detail"] for event in sent] == [
        {"tool": "status"},
        {"tool": "profile"},
        {"tool": "runs"},
    ]
    assert sent[0]["event"] == TOOL_CALLED


def test_the_stream_starts_after_the_cursor_it_is_given(client, services, sessions, scope, app):
    quick_streams(app)
    first, second, third = record(sessions, scope, "a", "b", "c")

    response = client.get(STREAM_URL, params={"after": first})

    assert [int(event["id"]) for event in streamed_events(response.text)] == [second, third]


def test_a_reconnecting_streams_last_event_id_wins_over_its_after(
    client, services, sessions, scope, app
):
    quick_streams(app)
    first, second, third = record(sessions, scope, "a", "b", "c")

    response = client.get(
        STREAM_URL, params={"after": first}, headers={"Last-Event-ID": str(second)}
    )

    assert [int(event["id"]) for event in streamed_events(response.text)] == [third]


def test_a_stream_without_a_cursor_starts_after_the_newest_event(
    client, services, sessions, scope, app
):
    quick_streams(app)
    ids = record(sessions, scope, "a", "b")

    assert streamed_events(client.get(STREAM_URL).text) == []
    sent = streamed_events(client.get(STREAM_URL, params={"after": 0}).text)
    assert [int(event["id"]) for event in sent] == ids


def test_a_proposal_waiting_is_announced_to_the_whole_workspace(
    client, services, sessions, scope, app
):
    quick_streams(app)
    elsewhere = second_project(sessions, scope)
    run = GraphRunRef(kind="research-run", run_id=uuid.uuid4())
    with transaction(sessions) as session:
        proposal = propose_action(
            session,
            scope=elsewhere,
            run=run,
            workflow_id=f"research-{run.run_id}",
            request=ActionRequest("research-run:1", "search_web", {"query": "pricing"}),
            effect=ActionEffect.SPENDS,
            wait_seconds=3600,
        )

    [sent] = streamed_events(client.get(STREAM_URL, params={"after": 0}).text)

    assert sent["event"] == "proposal_waiting"
    assert sent["data"]["detail"] == {
        "proposal_id": str(proposal.id),
        "action_name": "search_web",
        "effect": "spends",
    }


def test_a_cursor_of_the_stream_that_is_not_an_event_id_is_refused(
    client, services, sessions, scope, app
):
    quick_streams(app)

    assert client.get(STREAM_URL, params={"after": "x"}).status_code == 422
