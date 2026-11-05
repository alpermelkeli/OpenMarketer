"""Tests for the routes of the orchestrator's tasks: a real database, no workflow and no model.

They need PostgreSQL (see the root ``conftest.py``). Requests work in the test's session, which
is rolled back afterwards; tasks, their plans, steps and reports are stored with the core
functions the worker's hooks call.
"""

import uuid
from decimal import Decimal
from typing import Any, TypedDict

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from openmarketer_api.dependencies import db_session
from openmarketer_api.identity import current_workspace_id
from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.db.chat import create_chat
from openmarketer_core.db.models import Project, TaskStepOutcome, Workspace
from openmarketer_core.db.studio_runs import request_studio_run
from openmarketer_core.db.tasks import (
    mark_task_failed,
    mark_task_started,
    mark_task_succeeded,
    record_plan_change,
    record_step_dispatched,
    record_step_outcome,
    record_task_plan,
    request_task_run,
)
from openmarketer_core.orchestrator.task.plan import (
    TaskAgentName,
    TaskOutcome,
    TaskPlan,
    TaskReport,
    TaskRequest,
)
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.requests import AssetRequest

REQUEST = TaskRequest(
    request="Post about offline mode, with an image",
    understanding="One post for X announcing offline mode, with an image made for it.",
)
SCOPE: dict[str, Any] = {
    "max_steps": 6,
    "max_plan_changes": 2,
    "deadline_days": 7,
    "max_cost_usd": "1.00",
}
STUDIO_STEP: dict[str, Any] = {
    "id": "image",
    "agent": "studio",
    "request": {"goal": "Announce offline mode", "platform": "x_post"},
}
WRITING_STEP: dict[str, Any] = {
    "id": "post",
    "agent": "writing",
    "request": {"platform": "x", "goal": "introduce"},
    "depends_on": ["image"],
    "asset_from_step": "image",
}
FIRST_PLAN = TaskPlan.model_validate(
    {
        "goal": "Announce offline mode",
        "steps": [WRITING_STEP | {"depends_on": [], "asset_from_step": None}],
        "scope": SCOPE,
    }
)
PLAN = TaskPlan.model_validate(
    {"goal": "Announce offline mode", "steps": [STUDIO_STEP, WRITING_STEP], "scope": SCOPE}
)
ASSET_REQUEST = AssetRequest(goal="Announce offline mode", platform=Platform.X_POST)


def new_scope(session: Session) -> RunScope:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    return new_project(session, workspace.id)


def new_project(session: Session, workspace_id: uuid.UUID) -> RunScope:
    project = Project(
        workspace_id=workspace_id, name="App", source_repo_url="https://example.com/a.git"
    )
    session.add(project)
    session.flush()
    return RunScope(workspace_id=workspace_id, project_id=project.id)


@pytest.fixture
def scope(session) -> RunScope:
    return new_scope(session)


@pytest.fixture(autouse=True)
def database(app, session, scope) -> None:
    """Requests work in the test's session, in the workspace of ``scope``."""
    app.dependency_overrides[db_session] = lambda: session
    app.dependency_overrides[current_workspace_id] = lambda: scope.workspace_id


class ProjectIds(TypedDict):
    workspace_id: uuid.UUID
    project_id: uuid.UUID


def ids(scope: RunScope) -> ProjectIds:
    return ProjectIds(workspace_id=scope.workspace_id, project_id=scope.project_id)


def tasks_url(scope: RunScope, then: str = "") -> str:
    return f"/v1/projects/{scope.project_id}/tasks{then}"


def queued_task(session: Session, scope: RunScope, chat_id: uuid.UUID | None = None) -> uuid.UUID:
    return request_task_run(
        session, **ids(scope), request=REQUEST, requested_by="orchestrator", chat_thread_id=chat_id
    ).id


def finished_task(session: Session, scope: RunScope) -> tuple[uuid.UUID, uuid.UUID]:
    """A task whose plan changed once, whose studio step ran, and that ended with its report."""
    task_id = queued_task(session, scope)
    mark_task_started(session, **ids(scope), task_id=task_id)
    record_task_plan(session, **ids(scope), task_id=task_id, plan=FIRST_PLAN)
    record_plan_change(
        session, **ids(scope), task_id=task_id, plan=PLAN, reason="The post needs an image."
    )
    studio_run = request_studio_run(
        session, **ids(scope), request=ASSET_REQUEST, requested_by="run"
    )
    record_step_dispatched(
        session,
        **ids(scope),
        task_id=task_id,
        step_key="image",
        agent=TaskAgentName.STUDIO,
        child_run_id=studio_run.id,
    )
    record_step_outcome(
        session, **ids(scope), task_id=task_id, step_key="image", outcome=TaskStepOutcome.FAILED
    )
    mark_task_succeeded(
        session,
        **ids(scope),
        task_id=task_id,
        outcome=TaskOutcome.INCOMPLETE,
        report=TaskReport(summary="The image could not be made, so no post was written."),
        cost_usd=Decimal("0.042"),
    )
    return task_id, studio_run.id


def test_a_task_is_read_with_its_request_plan_changes_steps_and_report(
    client: TestClient, session, scope
):
    task_id, studio_run_id = finished_task(session, scope)

    response = client.get(tasks_url(scope, f"/{task_id}"))

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["outcome"], body["goal"], body["cost_usd"]) == (
        "succeeded",
        "incomplete",
        "Announce offline mode",
        0.042,
    )
    assert body["request"] == REQUEST.model_dump(mode="json")
    assert TaskPlan.model_validate(body["plan"]) == PLAN
    [change] = body["plan_changes"]
    assert change["reason"] == "The post needs an image."
    assert TaskPlan.model_validate(change["replaced"]) == FIRST_PLAN
    [step] = body["steps"]
    assert (step["step_id"], step["agent"], step["run_id"], step["outcome"]) == (
        "image",
        "studio",
        str(studio_run_id),
        "failed",
    )
    assert step["finished_at"] is not None
    assert body["report"]["summary"] == "The image could not be made, so no post was written."
    assert body["started_at"] is not None and body["finished_at"] is not None


def test_a_queued_task_has_no_plan_steps_or_report_yet(client: TestClient, session, scope):
    chat = create_chat(session, **ids(scope))
    task_id = queued_task(session, scope, chat.id)

    body = client.get(tasks_url(scope, f"/{task_id}")).json()

    assert (body["status"], body["outcome"], body["goal"], body["plan"], body["report"]) == (
        "queued",
        None,
        None,
        None,
        None,
    )
    assert (body["plan_changes"], body["steps"]) == ([], [])
    assert (body["requested_by"], body["chat_thread_id"]) == ("orchestrator", str(chat.id))


def test_a_failed_task_names_its_error(client: TestClient, session, scope):
    task_id = queued_task(session, scope)
    mark_task_failed(session, **ids(scope), task_id=task_id, error="the plan did not fit")

    body = client.get(tasks_url(scope, f"/{task_id}")).json()

    assert (body["status"], body["error"]) == ("failed", "the plan did not fit")


def test_the_list_shows_each_task_with_its_goal_and_without_its_plan(
    client: TestClient, session, scope
):
    task_id, _ = finished_task(session, scope)

    response = client.get(tasks_url(scope))

    assert response.status_code == 200
    [listed] = response.json()["tasks"]
    one = client.get(tasks_url(scope, f"/{task_id}")).json()
    left_out = {"request", "plan", "plan_changes", "steps", "report"}
    assert listed == {key: value for key, value in one.items() if key not in left_out}
    assert response.json()["next_cursor"] is None


def test_the_list_is_paged_newest_first(client: TestClient, session, scope):
    older = queued_task(session, scope)
    newer = queued_task(session, scope)
    # Both are stored in the test's one transaction, at the same time: the older is moved back.
    session.execute(
        text(
            "UPDATE orchestrator_task SET created_at = created_at - interval '1 minute' "
            "WHERE id = :id"
        ),
        {"id": older},
    )

    first = client.get(tasks_url(scope), params={"limit": 1}).json()
    second = client.get(
        tasks_url(scope), params={"limit": 1, "cursor": first["next_cursor"]}
    ).json()

    assert [task["id"] for task in first["tasks"] + second["tasks"]] == [str(newer), str(older)]
    assert second["next_cursor"] is None


def test_a_cursor_the_api_did_not_write_is_refused(client: TestClient, scope):
    assert client.get(tasks_url(scope), params={"cursor": "made-up"}).status_code == 422


def test_an_unknown_task_is_not_found(client: TestClient, scope):
    response = client.get(tasks_url(scope, f"/{uuid.uuid4()}"))

    assert (response.status_code, response.json()["code"]) == (404, "task_not_found")


def test_a_task_of_another_project_of_the_workspace_is_not_found(
    client: TestClient, session, scope
):
    task_id = queued_task(session, new_project(session, scope.workspace_id))

    response = client.get(tasks_url(scope, f"/{task_id}"))

    assert (response.status_code, response.json()["code"]) == (404, "task_not_found")


def test_tasks_of_a_project_of_another_workspace_are_not_found(client: TestClient, session):
    other = new_scope(session)
    task_id = queued_task(session, other)

    listing = client.get(tasks_url(other))
    one = client.get(tasks_url(other, f"/{task_id}"))

    assert (listing.status_code, listing.json()["code"]) == (404, "project_not_found")
    assert (one.status_code, one.json()["code"]) == (404, "task_not_found")


def test_a_stored_plan_that_no_longer_fits_is_answered_without_its_content(app, session, scope):
    task_id, _ = finished_task(session, scope)
    session.execute(
        text('UPDATE orchestrator_task SET plan = \'{"goal": "x"}\' WHERE id = :id'),
        {"id": task_id},
    )
    client = TestClient(
        app,
        base_url="http://localhost:8000",
        client=("127.0.0.1", 50000),
        raise_server_exceptions=False,
    )

    response = client.get(tasks_url(scope, f"/{task_id}"))

    assert (response.status_code, response.json()["code"]) == (500, "internal_error")
    assert "goal" not in response.json()["message"]
