"""Tests for the adapter the chat turn's request actions reach: a real database, a fake starter.

They need PostgreSQL (see the root ``conftest.py``). The adapter is core's ``ChatRequests`` for
one turn: it stores a research run, a studio run, a writing run or a task of the turn's project
and starts its workflow through the starter it was given, the way the routes do. Nothing
here reaches Temporal: the starter is a list (or a function that refuses), so what the adapter
asks it to start is read back whole.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_api.chat_requests import PostgresChatRequests
from openmarketer_core.db.chat import create_chat
from openmarketer_core.db.models import (
    Project,
    ResearchRunRecord,
    StudioRunRecord,
    TaskRecord,
    Workspace,
    WritingRunRecord,
)
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.orchestrator.requests import (
    ResearchNotStarted,
    StudioNotStarted,
    TaskNotStarted,
    WritingNotStarted,
)
from openmarketer_core.orchestrator.task.plan import TaskRequest
from openmarketer_core.orchestrator.task.workflow_contract import (
    TASK_TASK_QUEUE,
    TASK_WORKFLOW,
    TaskRunInput,
)
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
from openmarketer_core.writing.brief import WritingBrief, WritingGoal, WritingPlatform
from openmarketer_core.writing.workflow_contract import (
    WRITING_TASK_QUEUE,
    WRITING_WORKFLOW,
    WritingRunInput,
)

REQUEST = ResearchRequest(questions=["What did competitors post last month?"])
ASSET_REQUEST = AssetRequest(
    goal="Announce the Android launch", exact_copy=["Now on Android"], platform=Platform.X_POST
)

BRIEF = WritingBrief(platform=WritingPlatform.X, goal=WritingGoal.INTRODUCE, variants=2)

TASK_REQUEST = TaskRequest(
    request="Post about offline mode, with an image",
    understanding="One post for X announcing offline mode, with an image made for it.",
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


def test_starting_a_task_stores_it_for_the_chat_and_hands_its_workflow_to_the_starter(
    sessions, scope
):
    started: list[WorkflowStart] = []

    reading = adapter_of(sessions, scope, started.append).start_task(TASK_REQUEST)

    [workflow] = started
    assert (workflow.workflow, workflow.task_queue) == (TASK_WORKFLOW, TASK_TASK_QUEUE)
    assert workflow.input == TaskRunInput(
        workspace_id=scope[0], project_id=scope[1], run_id=reading.id
    )
    assert (reading.status.value, reading.requested_by, reading.request) == (
        "queued",
        "orchestrator",
        TASK_REQUEST.request,
    )
    assert (reading.goal, reading.plan, reading.steps, reading.report) == (None, None, [], None)
    with transaction(sessions) as session:
        stored = session.get(TaskRecord, reading.id)
    assert stored is not None
    assert (stored.requested_by, stored.understanding) == (
        "orchestrator",
        TASK_REQUEST.understanding,
    )
    assert stored.chat_thread_id == scope[2]


def test_a_task_workflow_that_cannot_be_started_is_the_ports_own_error_in_a_fixed_sentence(
    sessions, scope
):
    with pytest.raises(TaskNotStarted) as refused:
        adapter_of(sessions, scope, refusing).start_task(TASK_REQUEST)

    assert str(refused.value) == "the task could not be handed to a worker"
    with transaction(sessions) as session:
        status = session.scalar(select(TaskRecord.status).where(TaskRecord.project_id == scope[1]))
    assert status is not None
    assert status.value == "failed"


def test_starting_a_writing_run_stores_it_for_the_chat_and_hands_its_workflow_to_the_starter(
    sessions, scope
):
    started: list[WorkflowStart] = []

    reading = adapter_of(sessions, scope, started.append).start_writing(BRIEF)

    [workflow] = started
    assert (workflow.workflow, workflow.task_queue) == (WRITING_WORKFLOW, WRITING_TASK_QUEUE)
    assert workflow.input == WritingRunInput(
        workspace_id=scope[0], project_id=scope[1], run_id=reading.id
    )
    assert (reading.status.value, reading.requested_by, reading.platform) == (
        "queued",
        "orchestrator",
        "x",
    )
    assert reading.brief == BRIEF.model_dump(mode="json")
    with transaction(sessions) as session:
        stored = session.get(WritingRunRecord, reading.id)
    assert stored is not None
    assert (stored.requested_by, stored.chat_thread_id) == ("orchestrator", scope[2])


def test_a_writing_workflow_that_cannot_be_started_is_the_ports_own_error_in_a_fixed_sentence(
    sessions, scope
):
    with pytest.raises(WritingNotStarted) as refused:
        adapter_of(sessions, scope, refusing).start_writing(BRIEF)

    assert str(refused.value) == "the post could not be handed to a worker"
    with transaction(sessions) as session:
        status = session.scalar(
            select(WritingRunRecord.status).where(WritingRunRecord.project_id == scope[1])
        )
    assert status is not None
    assert status.value == "failed"


def test_an_asset_the_project_lacks_starts_no_writing_run(sessions, scope):
    started: list[WorkflowStart] = []
    brief = BRIEF.model_copy(update={"asset": uuid.uuid4()})

    with pytest.raises(WritingNotStarted) as refused:
        adapter_of(sessions, scope, started.append).start_writing(brief)

    assert str(refused.value) == "the post's asset is not in this project's Studio library"
    assert started == []
    with transaction(sessions) as session:
        stored = session.scalar(
            select(WritingRunRecord.id).where(WritingRunRecord.project_id == scope[1])
        )
    assert stored is None


def test_a_research_run_the_project_lacks_starts_no_writing_run(sessions, scope):
    started: list[WorkflowStart] = []
    brief = BRIEF.model_copy(update={"research_run_id": uuid.uuid4()})

    with pytest.raises(WritingNotStarted) as refused:
        adapter_of(sessions, scope, started.append).start_writing(brief)

    assert str(refused.value) == "the post's research run is not one of this project's"
    assert started == []


def test_a_research_run_that_has_not_succeeded_starts_no_writing_run(sessions, scope):
    started: list[WorkflowStart] = []
    adapter = adapter_of(sessions, scope, started.append)
    research = adapter.start_research(REQUEST)
    brief = BRIEF.model_copy(update={"research_run_id": research.id})

    with pytest.raises(WritingNotStarted) as refused:
        adapter.start_writing(brief)

    assert str(refused.value) == (
        "the post's research run has not succeeded, so it has no findings to use"
    )
    assert len(started) == 1
