"""Tests for the store of orchestrator tasks: a task moves forwards, with its plan and steps.

They need PostgreSQL (see the root ``conftest.py``). A task's plan, its changes, its dispatched
steps and its report are recorded only while it runs, each once; the store holds that a step is
a step of the plan in use and that its run is a run of its agent in the task's project. Which
plan is allowed, and when a step may be dispatched, are the task agent's rules, not tested here.
"""

import uuid
from decimal import Decimal
from typing import Any, TypedDict

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.db.chat import create_chat, delete_chat
from openmarketer_core.db.models import (
    Project,
    TaskRecord,
    TaskStatus,
    TaskStepOutcome,
    TaskStepRecord,
    Workspace,
)
from openmarketer_core.db.project_removal import ProjectHasUnfinishedTask, delete_project
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.research_runs import ResearchRunNotFound, request_research_run
from openmarketer_core.db.run_events import append_events, events_of_chat
from openmarketer_core.db.studio_runs import StudioRunNotFound, request_studio_run
from openmarketer_core.db.studio_runs import mark_run_failed as mark_studio_failed
from openmarketer_core.db.tasks import (
    TaskFinished,
    TaskHasNoPlan,
    TaskNotFound,
    TaskNotStarted,
    TaskPlanRecorded,
    TaskStepDispatched,
    TaskStepNotDispatched,
    TaskStepNotInPlan,
    TaskStepOutcomeRecorded,
    list_tasks,
    mark_task_failed,
    mark_task_started,
    mark_task_succeeded,
    orchestrator_task,
    record_plan_change,
    record_step_dispatched,
    record_step_outcome,
    record_task_plan,
    request_task_run,
    task_to_execute,
)
from openmarketer_core.orchestrator.task.plan import (
    StoredTaskInvalid,
    TaskAgentName,
    TaskOutcome,
    TaskPlan,
    TaskReport,
    TaskRequest,
)
from openmarketer_core.orchestrator.task.workflow_contract import TASK_RUN_KIND
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.run_events import RunEvent
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
PLAN = TaskPlan.model_validate(
    {"goal": "Announce offline mode", "steps": [STUDIO_STEP, WRITING_STEP], "scope": SCOPE}
)
CHANGED_PLAN = TaskPlan.model_validate(
    {
        "goal": "Announce offline mode",
        "steps": [WRITING_STEP | {"depends_on": [], "asset_from_step": None}],
        "scope": SCOPE,
    }
)
REPORT = TaskReport(summary="One post was proposed and approved.")
COMPLETED = TaskOutcome.COMPLETED


def new_scope(session: Session) -> RunScope:
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
def scope(session) -> RunScope:
    return new_scope(session)


class ProjectIds(TypedDict):
    workspace_id: uuid.UUID
    project_id: uuid.UUID


def ids(scope: RunScope) -> ProjectIds:
    return ProjectIds(workspace_id=scope.workspace_id, project_id=scope.project_id)


def queued_task(session: Session, scope: RunScope, chat_id: uuid.UUID | None = None) -> uuid.UUID:
    return request_task_run(
        session, **ids(scope), request=REQUEST, requested_by="orchestrator", chat_thread_id=chat_id
    ).id


def running_task(session: Session, scope: RunScope) -> uuid.UUID:
    task_id = queued_task(session, scope)
    mark_task_started(session, **ids(scope), task_id=task_id)
    return task_id


def planned_task(session: Session, scope: RunScope) -> uuid.UUID:
    task_id = running_task(session, scope)
    record_task_plan(session, **ids(scope), task_id=task_id, plan=PLAN)
    return task_id


def studio_run(session: Session, scope: RunScope) -> uuid.UUID:
    return request_studio_run(
        session,
        **ids(scope),
        request=AssetRequest(goal="Announce offline mode", platform=Platform.X_POST),
        requested_by="run",
        origin_action_id=f"action-{uuid.uuid4()}",
    ).id


def dispatched_task(session: Session, scope: RunScope) -> tuple[uuid.UUID, uuid.UUID]:
    task_id = planned_task(session, scope)
    run_id = studio_run(session, scope)
    record_step_dispatched(
        session,
        **ids(scope),
        task_id=task_id,
        step_key="image",
        agent=TaskAgentName.STUDIO,
        child_run_id=run_id,
    )
    return task_id, run_id


def test_a_requested_task_is_queued_with_its_request(session, scope):
    task_id = queued_task(session, scope)

    task = orchestrator_task(session, **ids(scope), task_id=task_id)
    assert (task.status, task.request, task.plan, task.steps) == (
        TaskStatus.QUEUED,
        REQUEST,
        None,
        (),
    )


def test_a_task_of_a_project_the_workspace_does_not_have_is_refused(session, scope):
    other = new_scope(session)

    with pytest.raises(ProjectNotFound):
        request_task_run(
            session,
            workspace_id=scope.workspace_id,
            project_id=other.project_id,
            request=REQUEST,
            requested_by="orchestrator",
        )


def test_a_task_of_another_workspace_is_not_found(session, scope):
    task_id = queued_task(session, scope)
    other = new_scope(session)

    with pytest.raises(TaskNotFound):
        orchestrator_task(
            session, workspace_id=other.workspace_id, project_id=scope.project_id, task_id=task_id
        )
    with pytest.raises(TaskNotFound):
        mark_task_started(
            session, workspace_id=other.workspace_id, project_id=scope.project_id, task_id=task_id
        )


def test_starting_twice_changes_nothing(session, scope):
    task_id = running_task(session, scope)
    first = orchestrator_task(session, **ids(scope), task_id=task_id)

    again = mark_task_started(session, **ids(scope), task_id=task_id)

    assert (again.status, again.started_at) == (TaskStatus.RUNNING, first.started_at)


def test_a_plan_is_recorded_only_for_a_running_task(session, scope):
    task_id = queued_task(session, scope)

    with pytest.raises(TaskNotStarted):
        record_task_plan(session, **ids(scope), task_id=task_id, plan=PLAN)


def test_the_same_plan_again_changes_nothing(session, scope):
    task_id = planned_task(session, scope)

    task = record_task_plan(session, **ids(scope), task_id=task_id, plan=PLAN)

    assert (task.plan, task.plan_changes) == (PLAN, ())


def test_another_first_plan_is_refused(session, scope):
    task_id = planned_task(session, scope)

    with pytest.raises(TaskPlanRecorded):
        record_task_plan(session, **ids(scope), task_id=task_id, plan=CHANGED_PLAN)


def test_a_change_keeps_the_plan_it_replaced_and_why(session, scope):
    task_id = planned_task(session, scope)

    task = record_plan_change(
        session, **ids(scope), task_id=task_id, plan=CHANGED_PLAN, reason="No image is needed."
    )

    assert task.plan == CHANGED_PLAN
    assert [(c.reason, c.replaced) for c in task.plan_changes] == [("No image is needed.", PLAN)]


def test_recording_the_plan_in_use_as_a_change_adds_none(session, scope):
    task_id = planned_task(session, scope)
    record_plan_change(session, **ids(scope), task_id=task_id, plan=CHANGED_PLAN, reason="Why.")

    task = record_plan_change(
        session, **ids(scope), task_id=task_id, plan=CHANGED_PLAN, reason="Why."
    )

    assert len(task.plan_changes) == 1


def test_a_plan_is_changed_only_once_it_has_one(session, scope):
    task_id = running_task(session, scope)

    with pytest.raises(TaskHasNoPlan):
        record_plan_change(session, **ids(scope), task_id=task_id, plan=PLAN, reason="Why.")


def test_a_dispatched_step_names_the_run_it_started(session, scope):
    task_id, run_id = dispatched_task(session, scope)

    [step] = orchestrator_task(session, **ids(scope), task_id=task_id).steps
    assert (step.step_key, step.agent, step.child_run_id, step.outcome) == (
        "image",
        TaskAgentName.STUDIO,
        run_id,
        None,
    )


def test_dispatching_the_same_run_again_changes_nothing(session, scope):
    task_id, run_id = dispatched_task(session, scope)

    task = record_step_dispatched(
        session,
        **ids(scope),
        task_id=task_id,
        step_key="image",
        agent=TaskAgentName.STUDIO,
        child_run_id=run_id,
    )

    assert len(task.steps) == 1


def test_a_step_starts_one_run(session, scope):
    task_id, _ = dispatched_task(session, scope)

    with pytest.raises(TaskStepDispatched):
        record_step_dispatched(
            session,
            **ids(scope),
            task_id=task_id,
            step_key="image",
            agent=TaskAgentName.STUDIO,
            child_run_id=studio_run(session, scope),
        )


@pytest.mark.parametrize(
    ("step_key", "agent"),
    [("caption", TaskAgentName.STUDIO), ("image", TaskAgentName.WRITING)],
)
def test_a_step_that_is_not_one_of_the_plan_is_refused(session, scope, step_key, agent):
    task_id = planned_task(session, scope)

    with pytest.raises(TaskStepNotInPlan):
        record_step_dispatched(
            session,
            **ids(scope),
            task_id=task_id,
            step_key=step_key,
            agent=agent,
            child_run_id=studio_run(session, scope),
        )


def test_a_step_names_a_run_of_its_agent(session, scope):
    task_id = planned_task(session, scope)
    research = request_research_run(
        session,
        **ids(scope),
        request=ResearchRequest(questions=["Who announced offline mode?"]),
        requested_by="run",
    )

    with pytest.raises(StudioRunNotFound):
        record_step_dispatched(
            session,
            **ids(scope),
            task_id=task_id,
            step_key="image",
            agent=TaskAgentName.STUDIO,
            child_run_id=research.id,
        )


def test_a_step_names_a_run_of_the_tasks_project(session, scope):
    task_id = planned_task(session, scope)
    other = new_scope(session)

    with pytest.raises(StudioRunNotFound):
        record_step_dispatched(
            session,
            **ids(scope),
            task_id=task_id,
            step_key="image",
            agent=TaskAgentName.STUDIO,
            child_run_id=studio_run(session, other),
        )


def test_a_research_step_of_no_research_run_is_refused(session, scope):
    research_plan = TaskPlan.model_validate(
        {
            "goal": "Find out",
            "steps": [{"id": "find", "agent": "research", "request": {"questions": ["Who?"]}}],
            "scope": SCOPE,
        }
    )
    task_id = running_task(session, scope)
    record_task_plan(session, **ids(scope), task_id=task_id, plan=research_plan)

    with pytest.raises(ResearchRunNotFound):
        record_step_dispatched(
            session,
            **ids(scope),
            task_id=task_id,
            step_key="find",
            agent=TaskAgentName.RESEARCH,
            child_run_id=uuid.uuid4(),
        )


def test_the_outcome_of_a_steps_run_is_recorded_once(session, scope):
    task_id, _ = dispatched_task(session, scope)
    record = {"task_id": task_id, "step_key": "image"}
    record_step_outcome(session, **ids(scope), **record, outcome=TaskStepOutcome.NEEDS_REVIEW)

    again = record_step_outcome(
        session, **ids(scope), **record, outcome=TaskStepOutcome.NEEDS_REVIEW
    )

    [step] = again.steps
    assert step.outcome is TaskStepOutcome.NEEDS_REVIEW
    assert step.finished_at is not None
    with pytest.raises(TaskStepOutcomeRecorded):
        record_step_outcome(session, **ids(scope), **record, outcome=TaskStepOutcome.FAILED)


def test_a_step_that_started_no_run_has_no_outcome(session, scope):
    task_id = planned_task(session, scope)

    with pytest.raises(TaskStepNotDispatched):
        record_step_outcome(
            session,
            **ids(scope),
            task_id=task_id,
            step_key="post",
            outcome=TaskStepOutcome.SUCCEEDED,
        )


def test_only_a_studio_step_needs_review(session, scope):
    task_id = planned_task(session, scope)
    session.add(
        TaskStepRecord(
            project_id=scope.project_id,
            task_id=task_id,
            step_key="post",
            agent="writing",
            child_run_id=uuid.uuid4(),
            outcome=TaskStepOutcome.NEEDS_REVIEW,
            finished_at=text("now()"),
        )
    )

    with pytest.raises(IntegrityError, match="ck_orchestrator_task_step_needs_review_of_studio"):
        session.flush()


def test_a_succeeded_task_keeps_its_report_and_cost(session, scope):
    task_id = planned_task(session, scope)

    task = mark_task_succeeded(
        session,
        **ids(scope),
        task_id=task_id,
        outcome=COMPLETED,
        report=REPORT,
        cost_usd=Decimal("0.12"),
    )

    assert (task.status, task.outcome, task.report, task.cost_usd) == (
        TaskStatus.SUCCEEDED,
        COMPLETED,
        REPORT,
        Decimal("0.12"),
    )
    assert task.finished_at is not None


def test_a_task_whose_plan_was_rejected_ends_with_its_report(session, scope):
    task_id = planned_task(session, scope)

    task = mark_task_succeeded(
        session,
        **ids(scope),
        task_id=task_id,
        outcome=TaskOutcome.PLAN_REJECTED,
        report=TaskReport(summary="The plan was rejected; nothing was started."),
        cost_usd=Decimal("0.01"),
    )

    assert (task.status, task.outcome) == (TaskStatus.SUCCEEDED, TaskOutcome.PLAN_REJECTED)
    [listed] = list_tasks(session, **ids(scope), limit=1).items
    assert listed.outcome is TaskOutcome.PLAN_REJECTED


def test_a_task_that_ended_takes_no_other_outcome(session, scope):
    task_id = planned_task(session, scope)
    mark_task_succeeded(
        session,
        **ids(scope),
        task_id=task_id,
        outcome=COMPLETED,
        report=REPORT,
        cost_usd=Decimal(0),
    )

    with pytest.raises(TaskFinished):
        mark_task_succeeded(
            session,
            **ids(scope),
            task_id=task_id,
            outcome=TaskOutcome.INCOMPLETE,
            report=REPORT,
            cost_usd=Decimal(0),
        )


def test_an_unfinished_task_has_no_outcome(session, scope):
    task_id = planned_task(session, scope)

    assert orchestrator_task(session, **ids(scope), task_id=task_id).outcome is None


def test_the_same_report_again_changes_nothing(session, scope):
    task_id = planned_task(session, scope)
    first = mark_task_succeeded(
        session,
        **ids(scope),
        task_id=task_id,
        outcome=COMPLETED,
        report=REPORT,
        cost_usd=Decimal("0.12"),
    )

    again = mark_task_succeeded(
        session,
        **ids(scope),
        task_id=task_id,
        outcome=COMPLETED,
        report=REPORT,
        cost_usd=Decimal("0.12"),
    )

    assert again == first


def test_a_finished_task_takes_nothing_more(session, scope):
    task_id = planned_task(session, scope)
    mark_task_succeeded(
        session,
        **ids(scope),
        task_id=task_id,
        outcome=COMPLETED,
        report=REPORT,
        cost_usd=Decimal(0),
    )

    with pytest.raises(TaskFinished):
        record_plan_change(session, **ids(scope), task_id=task_id, plan=CHANGED_PLAN, reason="W.")
    with pytest.raises(TaskFinished):
        mark_task_failed(session, **ids(scope), task_id=task_id, error="Too late.")
    with pytest.raises(TaskFinished):
        mark_task_succeeded(
            session,
            **ids(scope),
            task_id=task_id,
            outcome=COMPLETED,
            report=TaskReport(summary="Another."),
            cost_usd=Decimal(0),
        )


def test_a_task_without_a_plan_cannot_succeed(session, scope):
    task_id = running_task(session, scope)

    with pytest.raises(TaskHasNoPlan):
        mark_task_succeeded(
            session,
            **ids(scope),
            task_id=task_id,
            outcome=COMPLETED,
            report=REPORT,
            cost_usd=Decimal(0),
        )


def test_a_queued_task_cannot_succeed_but_can_fail(session, scope):
    task_id = queued_task(session, scope)

    with pytest.raises(TaskNotStarted):
        mark_task_succeeded(
            session,
            **ids(scope),
            task_id=task_id,
            outcome=COMPLETED,
            report=REPORT,
            cost_usd=Decimal(0),
        )
    failed = mark_task_failed(session, **ids(scope), task_id=task_id, error="Not handed over.")
    assert (failed.status, failed.started_at) == (TaskStatus.FAILED, None)


def test_failing_again_keeps_the_first_message(session, scope):
    task_id = running_task(session, scope)
    mark_task_failed(session, **ids(scope), task_id=task_id, error="First.")

    task = mark_task_failed(session, **ids(scope), task_id=task_id, error="Second.")

    assert task.error == "First."


def test_a_long_failure_is_shortened(session, scope):
    task_id = running_task(session, scope)

    task = mark_task_failed(session, **ids(scope), task_id=task_id, error="x" * 5000)

    assert task.error is not None and len(task.error) == 2000


ENDED = {"started_at": func.now(), "finished_at": func.now()}
STORED_PLAN = PLAN.model_dump(mode="json")
STORED_REPORT = REPORT.model_dump(mode="json")


@pytest.mark.parametrize(
    ("columns", "constraint"),
    [
        (
            {"status": TaskStatus.SUCCEEDED, "outcome": COMPLETED, "plan": STORED_PLAN, **ENDED},
            "ck_orchestrator_task_report",
        ),
        (
            {
                "status": TaskStatus.SUCCEEDED,
                "outcome": COMPLETED,
                "report": STORED_REPORT,
                **ENDED,
            },
            "ck_orchestrator_task_plan",
        ),
        (
            {"status": TaskStatus.SUCCEEDED, "plan": STORED_PLAN, "report": STORED_REPORT, **ENDED},
            "ck_orchestrator_task_outcome_of_end",
        ),
        ({"outcome": COMPLETED}, "ck_orchestrator_task_outcome_of_end"),
        ({"status": TaskStatus.FAILED, **ENDED}, "ck_orchestrator_task_error"),
        ({"status": TaskStatus.RUNNING}, "ck_orchestrator_task_started_at"),
        ({"requested_by": "model"}, "ck_orchestrator_task_requested_by"),
        ({"cost_usd": Decimal(-1)}, "ck_orchestrator_task_cost_usd"),
    ],
)
def test_an_inconsistent_task_is_refused(session, scope, columns, constraint):
    session.add(
        TaskRecord(
            **{
                "project_id": scope.project_id,
                "request": "r",
                "understanding": "u",
                **columns,
            }
        )
    )

    with pytest.raises(IntegrityError, match=constraint):
        session.flush()


def test_a_stored_plan_that_no_longer_fits_is_refused_with_the_task(session, scope):
    task_id = planned_task(session, scope)
    session.execute(
        text('UPDATE orchestrator_task SET plan = \'{"goal": ""}\' WHERE id = :id'),
        {"id": task_id},
    )

    with pytest.raises(StoredTaskInvalid):
        orchestrator_task(session, **ids(scope), task_id=task_id)


def test_the_worker_finds_a_task_by_its_id(session, scope):
    chat = create_chat(session, **ids(scope))
    task_id = queued_task(session, scope, chat.id)

    found = task_to_execute(session, task_id)

    assert found is not None
    assert (found.workspace_id, found.project_id, found.run_id, found.request) == (
        scope.workspace_id,
        scope.project_id,
        task_id,
        REQUEST,
    )
    assert (found.requested_by, found.chat_thread_id) == ("orchestrator", chat.id)
    assert found.created_at == orchestrator_task(session, **ids(scope), task_id=task_id).created_at
    assert task_to_execute(session, uuid.uuid4()) is None


def test_tasks_are_listed_newest_first_a_page_at_a_time(session, scope):
    older = planned_task(session, scope)
    newer = queued_task(session, scope)
    session.execute(
        text("UPDATE orchestrator_task SET created_at = now() - interval '1 hour' WHERE id = :id"),
        {"id": older},
    )

    first = list_tasks(session, **ids(scope), limit=1)
    second = list_tasks(session, **ids(scope), limit=1, before=first.next_before)

    assert [t.id for t in first.items] == [newer]
    assert [(t.id, t.goal) for t in second.items] == [(older, PLAN.goal)]
    assert second.next_before is None


def test_a_list_reads_neither_a_plan_nor_a_report(session, scope, statements):
    planned_task(session, scope)
    statements.clear()

    list_tasks(session, **ids(scope), limit=10)

    [listed] = [s for s in statements if "FROM orchestrator_task" in s]
    assert "orchestrator_task.plan," not in listed
    assert "orchestrator_task.report" not in listed
    assert "plan_changes" not in listed


def test_a_list_holds_only_the_projects_tasks(session, scope):
    queued_task(session, scope)
    other = new_scope(session)

    assert list_tasks(session, **ids(other), limit=10).items == ()


def test_a_project_with_a_task_in_progress_is_not_deleted(session, scope):
    running_task(session, scope)

    with pytest.raises(ProjectHasUnfinishedTask):
        delete_project(session, **ids(scope))


def test_a_project_is_deleted_with_its_finished_tasks_and_their_steps(session, scope):
    task_id, run_id = dispatched_task(session, scope)
    mark_task_failed(session, **ids(scope), task_id=task_id, error="Stopped.")
    mark_studio_failed(session, **ids(scope), run_id=run_id, error="Stopped.")

    delete_project(session, **ids(scope))

    assert session.scalar(select(func.count()).where(TaskRecord.id == task_id)) == 0
    assert session.scalar(select(func.count()).where(TaskStepRecord.task_id == task_id)) == 0


def test_a_deleted_chat_leaves_its_tasks_without_a_chat(session, scope):
    chat = create_chat(session, **ids(scope))
    task_id = queued_task(session, scope, chat.id)

    delete_chat(session, **ids(scope), chat_id=chat.id)

    assert orchestrator_task(session, **ids(scope), task_id=task_id).chat_thread_id is None


def test_a_chat_reads_the_events_of_the_tasks_it_started(session, scope):
    chat = create_chat(session, **ids(scope))
    task_id = queued_task(session, scope, chat.id)
    elsewhere = queued_task(session, scope)
    for run_id in (task_id, elsewhere):
        append_events(
            session,
            **ids(scope),
            run_kind=TASK_RUN_KIND,
            run_id=run_id,
            events=[RunEvent("task_planned", {})],
        )

    events = events_of_chat(session, **ids(scope), chat_id=chat.id, after=None, limit=10)

    assert [e.run_id for e in events] == [task_id]
