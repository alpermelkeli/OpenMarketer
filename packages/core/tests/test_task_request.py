"""Tests for requesting an orchestrator task, and the runs a task's actions ask for.

They need PostgreSQL (see the root ``conftest.py``). The requests commit their own
transactions, so rows are committed here too. No workflow engine is used: starting a workflow
is a function the test supplies. A run a task's action asks for is stored by its own kind's
use case, keyed by the action, with no chat and no workflow started: the runtime starts it as
the task's child.
"""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.chat import ChatNotFound
from openmarketer_core.db.models import Project, TaskStatus, Workspace
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.db.tasks import OrchestratorTask, orchestrator_task
from openmarketer_core.orchestrator.task.plan import TaskRequest
from openmarketer_core.orchestrator.task.request import (
    WORKFLOW_NOT_STARTED,
    TaskWorkflowNotStarted,
    request_task,
)
from openmarketer_core.orchestrator.task.workflow_contract import (
    TASK_TASK_QUEUE,
    TASK_WORKFLOW,
    TaskRunInput,
    task_workflow_id,
)
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.research.request import request_research_for_action
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.request import request_asset_for_action
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.workflow_start import WorkflowNotStarted, WorkflowStart
from openmarketer_core.writing.brief import WritingBrief
from openmarketer_core.writing.request import request_post_for_action

REQUEST = TaskRequest(
    request="Post about offline mode", understanding="One post for X announcing offline mode."
)


@pytest.fixture
def sessions(engine) -> sessionmaker[Session]:
    return session_factory(engine.url.render_as_string(hide_password=False))


@pytest.fixture
def workspace_id(sessions) -> uuid.UUID:
    with transaction(sessions) as session:
        workspace = Workspace(name="Acme")
        session.add(workspace)
        session.flush()
        return workspace.id


@pytest.fixture
def project_id(sessions, workspace_id) -> uuid.UUID:
    with transaction(sessions) as session:
        project = Project(
            workspace_id=workspace_id, name="App", source_repo_url="https://example.com/a.git"
        )
        session.add(project)
        session.flush()
        return project.id


def stored(sessions, workspace_id: uuid.UUID, project_id: uuid.UUID, task_id) -> OrchestratorTask:
    with transaction(sessions) as session:
        return orchestrator_task(
            session, workspace_id=workspace_id, project_id=project_id, task_id=task_id
        )


def unreachable(_: WorkflowStart) -> None:
    raise WorkflowNotStarted("connection refused")


def test_a_requested_task_is_queued_and_its_workflow_started(sessions, workspace_id, project_id):
    started: list[WorkflowStart] = []

    task = request_task(
        sessions,
        workspace_id=workspace_id,
        project_id=project_id,
        request=REQUEST,
        start_workflow=started.append,
    )

    assert (task.status, task.requested_by) == (TaskStatus.QUEUED, "orchestrator")
    assert started == [
        WorkflowStart(
            workflow=TASK_WORKFLOW,
            workflow_id=task_workflow_id(task.id),
            task_queue=TASK_TASK_QUEUE,
            input=TaskRunInput(workspace_id=workspace_id, project_id=project_id, run_id=task.id),
        )
    ]
    assert stored(sessions, workspace_id, project_id, task.id) == task


def test_a_task_whose_workflow_cannot_start_is_failed(sessions, workspace_id, project_id):
    with pytest.raises(TaskWorkflowNotStarted) as raised:
        request_task(
            sessions,
            workspace_id=workspace_id,
            project_id=project_id,
            request=REQUEST,
            start_workflow=unreachable,
        )

    task = stored(sessions, workspace_id, project_id, raised.value.task_id)
    assert (task.status, task.error) == (TaskStatus.FAILED, WORKFLOW_NOT_STARTED)


def test_a_task_names_a_chat_of_its_project(sessions, workspace_id, project_id):
    with pytest.raises(ChatNotFound):
        request_task(
            sessions,
            workspace_id=workspace_id,
            project_id=project_id,
            request=REQUEST,
            start_workflow=lambda _: None,
            chat_thread_id=uuid.uuid4(),
        )


def research(sessions, scope: dict[str, uuid.UUID], action_id: str) -> Any:
    return request_research_for_action(
        sessions, **scope, request=ResearchRequest(questions=["Who?"]), action_id=action_id
    )


def asset(sessions, scope: dict[str, uuid.UUID], action_id: str) -> Any:
    return request_asset_for_action(
        sessions,
        **scope,
        request=AssetRequest(goal="Announce offline mode", platform=Platform.X_POST),
        action_id=action_id,
    )


def post(sessions, scope: dict[str, uuid.UUID], action_id: str) -> Any:
    return request_post_for_action(
        sessions,
        **scope,
        brief=WritingBrief.model_validate({"platform": "x", "goal": "introduce"}),
        action_id=action_id,
    )


CHILD_RUNS = [research, asset, post]


@pytest.mark.parametrize("request_for_action", CHILD_RUNS)
def test_a_run_an_action_asks_for_is_asked_for_by_a_run_with_no_chat(
    request_for_action: Callable[..., Any], sessions, workspace_id, project_id
):
    scope = {"workspace_id": workspace_id, "project_id": project_id}

    run = request_for_action(sessions, scope, f"task:{uuid.uuid4()}:1")

    assert (run.requested_by, run.chat_thread_id, run.status.value) == ("run", None, "queued")


@pytest.mark.parametrize("request_for_action", CHILD_RUNS)
def test_a_retried_action_finds_the_run_it_asked_for(
    request_for_action: Callable[..., Any], sessions, workspace_id, project_id
):
    scope = {"workspace_id": workspace_id, "project_id": project_id}
    action_id = f"task:{uuid.uuid4()}:1"

    first = request_for_action(sessions, scope, action_id)
    again = request_for_action(sessions, scope, action_id)
    other = request_for_action(sessions, scope, f"{action_id}-other")

    assert again.id == first.id
    assert other.id != first.id


@pytest.mark.parametrize("request_for_action", CHILD_RUNS)
def test_a_run_an_action_asks_for_belongs_to_the_workspaces_project(
    request_for_action: Callable[..., Any], sessions, workspace_id
):
    scope = {"workspace_id": workspace_id, "project_id": uuid.uuid4()}

    with pytest.raises(ProjectNotFound):
        request_for_action(sessions, scope, f"task:{uuid.uuid4()}:1")
