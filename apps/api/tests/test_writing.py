"""Tests for the writer's routes: a real database and a recording workflow starter.

They need PostgreSQL (see the root ``conftest.py``). Each test works in a workspace of its
own, committed, since a route commits its own transaction. Runs and their content items are
stored with the core functions the worker calls; no workflow runs and no model is called.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_api.dependencies import Services
from openmarketer_api.identity import LOCAL_USER_ID, current_workspace_id
from openmarketer_core.agent_runtime.actions import ActionEffect, ActionRequest
from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.db.content_items import (
    NewContentItem,
    settle_content_item,
    store_proposed_content_item,
)
from openmarketer_core.db.models import (
    ContentItemStatus,
    ProductProfileRecord,
    Project,
    Workspace,
    WritingRunRecord,
    WritingRunStatus,
)
from openmarketer_core.db.proposals import propose_action
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.db.writing_runs import (
    mark_run_started,
    mark_run_succeeded,
    request_writing_run,
)
from openmarketer_core.workflow_start import WorkflowNotStarted, WorkflowStart
from openmarketer_core.writing.brief import WritingBrief
from openmarketer_core.writing.workflow_contract import (
    WRITING_RUN_KIND,
    WRITING_TASK_QUEUE,
    WRITING_WORKFLOW,
    WritingRunInput,
)

BRIEF = {"platform": "x", "goal": "angle", "angle": "Offline first, for people on trains."}
POST = "Memoria keeps your notes in sync, offline too."
ITEM = NewContentItem(
    text=POST,
    plan={"angle": "offline first", "feature_ids": ["sync"], "format": "single"},
    plan_changes=["dropped a feature that is not live"],
    alternatives=[{"index": 1, "variant": {"text": "Notes that sync."}}],
    claims=[
        {
            "statement": "syncs offline",
            "feature_ids": ["sync"],
            "evidence": [{"feature_id": "sync", "file": "src/sync.ts", "lines": "1-20"}],
        }
    ],
    check_report={"passed": True, "failures": [], "length": 47, "flagged_numbers": []},
    critic_report={"passed": True, "findings": []},
    flagged_numbers=["3x"],
    cost_usd=Decimal("0.021"),
)


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


@dataclass(frozen=True)
class StoredPost:
    run_id: uuid.UUID
    item_id: uuid.UUID
    proposal_id: uuid.UUID


@pytest.fixture
def sessions(engine) -> sessionmaker[Session]:
    return session_factory(engine.url.render_as_string(hide_password=False))


def new_project(session: Session, workspace_id: uuid.UUID) -> RunScope:
    project = Project(
        workspace_id=workspace_id, name="App", source_repo_url="https://example.com/a.git"
    )
    session.add(project)
    session.flush()
    return RunScope(workspace_id=workspace_id, project_id=project.id)


def new_scope(sessions: sessionmaker[Session]) -> RunScope:
    with transaction(sessions) as session:
        workspace = Workspace(name="Acme")
        session.add(workspace)
        session.flush()
        return new_project(session, workspace.id)


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
    return f"/v1/projects/{scope.project_id}/writing-runs{then}"


def items_url(scope: RunScope, then: str = "") -> str:
    return f"/v1/projects/{scope.project_id}/content-items{then}"


def ids(scope: RunScope) -> dict[str, uuid.UUID]:
    return {"workspace_id": scope.workspace_id, "project_id": scope.project_id}


def queued_run(sessions: sessionmaker[Session], scope: RunScope) -> uuid.UUID:
    with transaction(sessions) as session:
        return request_writing_run(
            session, **ids(scope), brief=WritingBrief.model_validate(BRIEF), requested_by="person"
        ).id


def approved_version(session: Session, project_id: uuid.UUID) -> uuid.UUID:
    """An approved version of the project's profile: inserted as a draft, approved by UPDATE."""
    number = 1 + len(
        session.scalars(
            select(ProductProfileRecord.id).where(ProductProfileRecord.project_id == project_id)
        ).all()
    )
    record = ProductProfileRecord(
        project_id=project_id, version=number, content={"product": {"name": "App"}}
    )
    session.add(record)
    session.flush()
    session.execute(
        text(
            "UPDATE product_profile SET status = 'approved', approved_by = :by,"
            " approved_at = now() WHERE id = :id"
        ),
        {"by": LOCAL_USER_ID, "id": record.id},
    )
    return record.id


def proposed_post(
    sessions: sessionmaker[Session], scope: RunScope, *, settled: bool = False
) -> StoredPost:
    """A run that read the profile's first version and proposed its post, held by the gate.

    With ``settled``, the post was approved and recorded as a dry run, and the run succeeded.
    """
    run_id = queued_run(sessions, scope)
    with transaction(sessions) as session:
        version_id = approved_version(session, scope.project_id)
        mark_run_started(session, **ids(scope), run_id=run_id, profile_version_id=version_id)
        item = store_proposed_content_item(session, **ids(scope), run_id=run_id, item=ITEM)
        proposal = propose_action(
            session,
            scope=scope,
            run=GraphRunRef(kind=WRITING_RUN_KIND, run_id=run_id),
            workflow_id=f"writing-{run_id}",
            request=ActionRequest(
                action_id=f"{WRITING_RUN_KIND}:{run_id}:publish",
                name="publish_post",
                payload={"run_id": str(run_id), "content_item_id": str(item.id)},
            ),
            effect=ActionEffect.PUBLISHES,
            wait_seconds=3600,
        )
        if settled:
            settle_content_item(
                session,
                **ids(scope),
                item_id=item.id,
                status=ContentItemStatus.DRY_RUN,
                proposal_id=proposal.id,
            )
            mark_run_succeeded(session, **ids(scope), run_id=run_id, cost_usd=Decimal("0.03"))
    return StoredPost(run_id=run_id, item_id=item.id, proposal_id=proposal.id)


# ------------------------------------------------------------------------------- start


def test_start_answers_202_and_starts_the_workflow_with_identifiers_only(
    client: TestClient, services, scope, workflows
):
    response = client.post(runs_url(scope), json=BRIEF)

    assert response.status_code == 202
    body = response.json()
    assert (body["status"], body["platform"], body["requested_by"]) == ("queued", "x", "person")
    assert (body["profile_version"], body["content_item_id"], body["cost_usd"]) == (None, None, 0)
    [started] = workflows.started
    run_id = uuid.UUID(body["id"])
    assert (started.workflow, started.task_queue, started.workflow_id) == (
        WRITING_WORKFLOW,
        WRITING_TASK_QUEUE,
        f"writing-{run_id}",
    )
    assert started.input == WritingRunInput(
        workspace_id=scope.workspace_id, project_id=scope.project_id, run_id=run_id
    )


def test_a_run_that_cannot_be_started_is_failed_and_answered_503(
    client: TestClient, services, scope, workflows
):
    workflows.reachable = False

    response = client.post(runs_url(scope), json=BRIEF)

    assert (response.status_code, response.json()["code"]) == (503, "writing_not_started")
    [run] = client.get(runs_url(scope)).json()["runs"]
    assert (run["status"], run["error"]) == (
        WritingRunStatus.FAILED,
        "the post could not be handed to a worker",
    )


def test_start_refuses_unknown_fields(client: TestClient, services, scope, workflows):
    assert client.post(runs_url(scope), json={**BRIEF, "approved": True}).status_code == 422
    assert workflows.started == []


def test_start_refuses_a_brief_whose_goal_lacks_its_field(
    client: TestClient, services, scope, workflows
):
    response = client.post(runs_url(scope), json={"platform": "x", "goal": "announce"})
    assert response.status_code == 422
    assert workflows.started == []


def test_start_refuses_a_platform_the_writer_does_not_write_for(
    client: TestClient, services, scope, workflows
):
    assert client.post(runs_url(scope), json={**BRIEF, "platform": "linkedin"}).status_code == 422
    assert workflows.started == []


def test_start_for_a_project_of_another_workspace_is_not_found(
    client: TestClient, services, sessions, workflows
):
    other = new_scope(sessions)
    response = client.post(runs_url(other), json=BRIEF)
    assert (response.status_code, response.json()["code"]) == (404, "project_not_found")
    assert workflows.started == []


# --------------------------------------------------------------------------------- runs


def test_list_and_get_show_a_run_as_it_is(client: TestClient, services, scope, sessions):
    post = proposed_post(sessions, scope, settled=True)

    [listed] = client.get(runs_url(scope)).json()["runs"]
    one = client.get(runs_url(scope, f"/{post.run_id}")).json()

    assert listed == {key: value for key, value in one.items() if key != "brief"}
    assert (one["status"], one["content_item_id"], one["profile_version"], one["cost_usd"]) == (
        "succeeded",
        str(post.item_id),
        1,
        0.03,
    )
    assert "profile_version_id" not in one


def test_a_run_is_read_with_its_brief(client: TestClient, services, scope, sessions):
    run_id = queued_run(sessions, scope)

    brief = client.get(runs_url(scope, f"/{run_id}")).json()["brief"]

    assert brief == {**BRIEF, "feature": None, "variants": 3}


def test_a_stored_brief_that_no_longer_fits_is_answered_as_none(
    client: TestClient, services, scope, sessions
):
    run_id = queued_run(sessions, scope)
    with transaction(sessions) as session:
        session.execute(
            update(WritingRunRecord)
            .where(WritingRunRecord.id == run_id)
            .values(brief={**BRIEF, "image": "a launch card"})
        )

    response = client.get(runs_url(scope, f"/{run_id}"))

    assert (response.status_code, response.json()["brief"]) == (200, None)


def test_run_list_is_paged(client: TestClient, services, scope, sessions):
    newer, older = queued_run(sessions, scope), queued_run(sessions, scope)
    first = client.get(runs_url(scope), params={"limit": 1}).json()
    second = client.get(runs_url(scope), params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert {first["runs"][0]["id"], second["runs"][0]["id"]} == {str(newer), str(older)}
    assert second["next_cursor"] is None


def test_a_cursor_the_api_did_not_write_is_refused(client: TestClient, services, scope):
    assert client.get(runs_url(scope), params={"cursor": "made-up"}).status_code == 422
    assert client.get(items_url(scope), params={"cursor": "made-up"}).status_code == 422


def test_an_unknown_run_is_not_found(client: TestClient, services, scope):
    response = client.get(runs_url(scope, f"/{uuid.uuid4()}"))
    assert (response.status_code, response.json()["code"]) == (404, "writing_run_not_found")


def test_a_run_of_another_project_of_the_workspace_is_not_found(
    client: TestClient, services, scope, sessions
):
    with transaction(sessions) as session:
        sibling = new_project(session, scope.workspace_id)
    run_id = queued_run(sessions, sibling)

    response = client.get(runs_url(scope, f"/{run_id}"))

    assert (response.status_code, response.json()["code"]) == (404, "writing_run_not_found")


def test_runs_of_a_project_of_another_workspace_are_not_found(
    client: TestClient, services, sessions
):
    other = new_scope(sessions)
    run_id = queued_run(sessions, other)

    assert client.get(runs_url(other)).json()["code"] == "project_not_found"
    response = client.get(runs_url(other, f"/{run_id}"))
    assert (response.status_code, response.json()["code"]) == (404, "writing_run_not_found")


# ------------------------------------------------------------------------ content items


def test_an_item_is_read_with_the_record_of_how_it_was_written(
    client: TestClient, services, scope, sessions
):
    post = proposed_post(sessions, scope)

    item = client.get(items_url(scope, f"/{post.item_id}")).json()

    assert item == {
        "id": str(post.item_id),
        "project_id": str(scope.project_id),
        "run_id": str(post.run_id),
        "platform": "x",
        "status": "proposed",
        "text": POST,
        "profile_version": 1,
        "proposal_id": None,
        "cost_usd": 0.021,
        "created_at": item["created_at"],
        "settled_at": None,
        "plan": dict(ITEM.plan),
        "plan_changes": list(ITEM.plan_changes),
        "alternatives": list(ITEM.alternatives),
        "claims": list(ITEM.claims),
        "check_report": dict(ITEM.check_report),
        "critic_report": dict(ITEM.critic_report),
        "flagged_numbers": ["3x"],
    }


def test_a_settled_item_names_its_proposal_and_how_it_ended(
    client: TestClient, services, scope, sessions
):
    post = proposed_post(sessions, scope, settled=True)

    item = client.get(items_url(scope, f"/{post.item_id}")).json()

    assert (item["status"], item["proposal_id"]) == ("dry_run", str(post.proposal_id))
    assert item["settled_at"] is not None


def test_the_list_of_items_leaves_out_how_each_was_written(
    client: TestClient, services, scope, sessions
):
    post = proposed_post(sessions, scope)

    [listed] = client.get(items_url(scope)).json()["items"]
    one = client.get(items_url(scope, f"/{post.item_id}")).json()

    assert listed == {key: value for key, value in one.items() if key in listed}
    assert set(one) - set(listed) == {
        "plan",
        "plan_changes",
        "alternatives",
        "claims",
        "check_report",
        "critic_report",
        "flagged_numbers",
    }


def test_item_list_is_paged(client: TestClient, services, scope, sessions):
    newer, older = proposed_post(sessions, scope), proposed_post(sessions, scope)
    first = client.get(items_url(scope), params={"limit": 1}).json()
    second = client.get(
        items_url(scope), params={"limit": 1, "cursor": first["next_cursor"]}
    ).json()
    assert {first["items"][0]["id"], second["items"][0]["id"]} == {
        str(newer.item_id),
        str(older.item_id),
    }
    assert second["next_cursor"] is None


def test_an_unknown_item_is_not_found(client: TestClient, services, scope):
    response = client.get(items_url(scope, f"/{uuid.uuid4()}"))
    assert (response.status_code, response.json()["code"]) == (404, "content_item_not_found")


def test_an_item_of_another_project_of_the_workspace_is_not_found(
    client: TestClient, services, scope, sessions
):
    with transaction(sessions) as session:
        sibling = new_project(session, scope.workspace_id)
    post = proposed_post(sessions, sibling)

    response = client.get(items_url(scope, f"/{post.item_id}"))

    assert (response.status_code, response.json()["code"]) == (404, "content_item_not_found")


def test_items_of_a_project_of_another_workspace_are_not_found(
    client: TestClient, services, sessions
):
    other = new_scope(sessions)
    post = proposed_post(sessions, other)

    assert client.get(items_url(other)).json()["code"] == "project_not_found"
    response = client.get(items_url(other, f"/{post.item_id}"))
    assert (response.status_code, response.json()["code"]) == (404, "content_item_not_found")
