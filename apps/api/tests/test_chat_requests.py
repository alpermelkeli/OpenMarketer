"""Tests for the adapter the chat turn's request actions reach: a real database, a fake starter.

They need PostgreSQL (see the root ``conftest.py``). The adapter is core's ``ChatRequests`` for
one turn: it stores a research run or a studio run of the turn's project and starts its
workflow through the starter it was given, the way the research and studio routes do. Nothing
here reaches Temporal: the starter is a list (or a function that refuses), so what the adapter
asks it to start is read back whole.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_api.chat_requests import PostgresChatRequests
from openmarketer_core.db.chat import create_chat
from openmarketer_core.db.models import Project, ResearchRunRecord, StudioRunRecord, Workspace
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.orchestrator.requests import ResearchNotStarted, StudioNotStarted
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.research.workflow_contract import (
    RESEARCH_TASK_QUEUE,
    RESEARCH_WORKFLOW,
    ResearchRunInput,
)
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.studio.workflow_contract import (
    STUDIO_TASK_QUEUE,
    STUDIO_WORKFLOW,
    StudioRunInput,
)
from openmarketer_core.workflow_start import WorkflowNotStarted, WorkflowStart

REQUEST = ResearchRequest(questions=["What did competitors post last month?"])
ASSET_REQUEST = AssetRequest(
    goal="Announce the Android launch", exact_copy=["Now on Android"], platform=Platform.X_POST
)


@pytest.fixture
def sessions(engine) -> sessionmaker[Session]:
    return session_factory(engine.url.render_as_string(hide_password=False))


@pytest.fixture
def scope(sessions) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """The workspace, the project and the chat whose turn asks for research."""
    with transaction(sessions) as session:
        workspace = Workspace(name="Acme")
        session.add(workspace)
        session.flush()
        project = Project(
            workspace_id=workspace.id, name="App", source_repo_url="https://example.com/a.git"
        )
        session.add(project)
        session.flush()
        chat = create_chat(session, workspace_id=workspace.id, project_id=project.id)
        return workspace.id, project.id, chat.id


def adapter_of(sessions, scope, start_workflow) -> PostgresChatRequests:
    return PostgresChatRequests(
        sessions=sessions,
        start_workflow=start_workflow,
        workspace_id=scope[0],
        project_id=scope[1],
        chat_id=scope[2],
    )


def refusing(workflow: WorkflowStart) -> None:
    raise WorkflowNotStarted("Temporal is not reachable")


def test_starting_a_research_run_stores_it_and_hands_its_workflow_to_the_starter(sessions, scope):
    started: list[WorkflowStart] = []
    adapter = PostgresChatRequests(
        sessions=sessions,
        start_workflow=started.append,
        workspace_id=scope[0],
        project_id=scope[1],
        chat_id=scope[2],
    )

    reading = adapter.start_research(REQUEST)

    [workflow] = started
    assert (workflow.workflow, workflow.task_queue) == (RESEARCH_WORKFLOW, RESEARCH_TASK_QUEUE)
    assert isinstance(workflow.input, ResearchRunInput)
    assert reading.id == workflow.input.run_id
    assert (reading.status.value, reading.requested_by) == ("queued", "orchestrator")
    with transaction(sessions) as session:
        stored = session.get(ResearchRunRecord, reading.id)
    assert stored is not None
    assert (stored.requested_by, stored.questions) == ("orchestrator", list(REQUEST.questions))
    assert stored.chat_thread_id == scope[2]


def test_a_workflow_that_cannot_be_started_is_the_ports_own_error(sessions, scope):
    def refusing(workflow: WorkflowStart) -> None:
        raise WorkflowNotStarted("Temporal is not reachable")

    adapter = PostgresChatRequests(
        sessions=sessions,
        start_workflow=refusing,
        workspace_id=scope[0],
        project_id=scope[1],
        chat_id=scope[2],
    )

    with pytest.raises(ResearchNotStarted, match="could not be handed to a worker"):
        adapter.start_research(REQUEST)

    # The run was recorded as failed: it will never be executed, and a new one can be started.
    with transaction(sessions) as session:
        status = session.scalar(
            select(ResearchRunRecord.status).where(ResearchRunRecord.project_id == scope[1])
        )
    assert status is not None
    assert status.value == "failed"


def test_starting_a_studio_run_stores_it_and_hands_its_workflow_to_the_starter(sessions, scope):
    started: list[WorkflowStart] = []

    reading = adapter_of(sessions, scope, started.append).start_creative(ASSET_REQUEST)

    [workflow] = started
    assert (workflow.workflow, workflow.task_queue) == (STUDIO_WORKFLOW, STUDIO_TASK_QUEUE)
    assert isinstance(workflow.input, StudioRunInput)
    assert reading.id == workflow.input.run_id
    assert (reading.status.value, reading.requested_by, reading.platform) == (
        "queued",
        "orchestrator",
        "x_post",
    )
    assert (reading.goal, reading.exact_copy) == (ASSET_REQUEST.goal, ["Now on Android"])
    with transaction(sessions) as session:
        stored = session.get(StudioRunRecord, reading.id)
    assert stored is not None
    assert stored.requested_by == "orchestrator"
    assert stored.request["exact_copy"] == ["Now on Android"]
    assert stored.chat_thread_id == scope[2]


def test_a_studio_workflow_that_cannot_be_started_is_the_ports_own_error(sessions, scope):
    with pytest.raises(StudioNotStarted, match="could not be handed to a worker"):
        adapter_of(sessions, scope, refusing).start_creative(ASSET_REQUEST)

    with transaction(sessions) as session:
        status = session.scalar(
            select(StudioRunRecord.status).where(StudioRunRecord.project_id == scope[1])
        )
    assert status is not None
    assert status.value == "failed"


def test_a_reference_image_the_project_lacks_starts_nothing(sessions, scope):
    started: list[WorkflowStart] = []
    request = ASSET_REQUEST.model_copy(update={"reference_asset_ids": [uuid.uuid4()]})

    with pytest.raises(StudioNotStarted, match="Studio library"):
        adapter_of(sessions, scope, started.append).start_creative(request)

    assert started == []
    with transaction(sessions) as session:
        stored = session.scalar(
            select(StudioRunRecord.id).where(StudioRunRecord.project_id == scope[1])
        )
    assert stored is None
