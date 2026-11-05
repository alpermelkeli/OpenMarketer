"""Tests for what ``task.py`` does for a task: the confirmation, the runs a step starts, and
what a finished task stores. Plain functions against PostgreSQL; skipped when it is not there."""

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from task_fakes import COPY, TaskDatabase, a_post_with_an_image_plan
from temporalio.exceptions import ApplicationError
from writer_fakes import WriterDatabase  # pyright: ignore[reportMissingImports]

from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.db.models import TaskStatus, TaskStepOutcome
from openmarketer_core.db.research_runs import research_run
from openmarketer_core.db.run_events import events_of_chat
from openmarketer_core.db.session import transaction
from openmarketer_core.db.studio_runs import studio_run
from openmarketer_core.db.tasks import mark_task_started, record_task_plan, run_thread_id
from openmarketer_core.db.writing_runs import run_to_execute
from openmarketer_core.orchestrator.task.plan import TaskOutcome, TaskPlan, TaskRequest
from openmarketer_core.orchestrator.task.task_agent.definitions import (
    ConfirmPlan,
    RunResearch,
    RunStudio,
    RunWriting,
    initial_state,
)
from openmarketer_core.orchestrator.task.task_agent.rules import TaskLimits
from openmarketer_core.orchestrator.task.workflow_contract import TASK_RUN_KIND
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.research.request import REQUESTED_BY_A_RUN
from openmarketer_core.run_events import RUN_STARTED
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.writing.brief import WritingBrief, WritingGoal, WritingPlatform
from openmarketer_worker.agent_runtime.routes import ActionContext, RunContext
from openmarketer_worker.orchestrator_task import steps
from openmarketer_worker.orchestrator_task.task import step_run_outcome

RESEARCH = ResearchRequest(questions=["How do note apps announce offline sync?"])
IMAGE = AssetRequest(goal="Notes on a plane.", exact_copy=[COPY], platform=Platform.X_POST)


def the_plan() -> TaskPlan:
    """A plan of a research step, a studio step and a writing step that takes both."""
    plan = json.loads(a_post_with_an_image_plan().text)
    research = {"id": "research", "agent": "research", "request": RESEARCH.model_dump()}
    return TaskPlan.model_validate({**plan, "steps": [research, *plan["steps"]]})


@pytest.fixture
def running(task_database: TaskDatabase, sessions):
    """A task a worker picked up, with its plan recorded, as its first steps leave it."""
    asked = task_database.requested()
    with transaction(sessions) as session:
        ids = {"workspace_id": asked.run.workspace_id, "project_id": asked.run.project_id}
        mark_task_started(session, **ids, task_id=asked.run.run_id)
        record_task_plan(session, **ids, task_id=asked.run.run_id, plan=the_plan())
    return asked


def context_of(asked, interrupt: str = "1") -> RunContext:
    return RunContext(
        action_id=f"{run_thread_id(asked.run.run_id)}:{interrupt}",
        scope=RunScope(workspace_id=asked.run.workspace_id, project_id=asked.run.project_id),
    )


def ids_of(asked) -> dict[str, uuid.UUID]:
    return {"workspace_id": asked.run.workspace_id, "project_id": asked.run.project_id}


# --------------------------------------------------------------------- the confirmation
def test_confirming_the_plan_does_nothing_and_answers_nothing(work, running):
    payload = ConfirmPlan(task_id=running.run.run_id, confirmation=1, plan=the_plan())

    assert work.confirm_plan(payload, ActionContext("a:1")) == {}


# ---------------------------------------------------------------- the runs of the steps
def test_a_research_step_creates_its_run_asked_by_a_run_and_records_its_dispatch(
    work, running, task_database, sessions
):
    payload = RunResearch(task_id=running.run.run_id, step="research", request=RESEARCH)

    child = work.prepare_research(payload, context_of(running))

    with transaction(sessions) as session:
        made = research_run(session, **ids_of(running), run_id=child)
    assert (made.requested_by, made.chat_thread_id) == (REQUESTED_BY_A_RUN, None)
    [step] = task_database.task(running.run).steps
    assert (step.step_key, step.child_run_id, step.outcome) == ("research", child, None)


def test_a_step_prepared_again_finds_the_run_it_made_and_shows_its_start_once(
    work, running, task_database, sessions
):
    payload = RunStudio(task_id=running.run.run_id, step="image", request=IMAGE)

    first = work.prepare_studio(payload, context_of(running))
    again = work.prepare_studio(payload, context_of(running))

    assert first == again
    with transaction(sessions) as session:
        assert studio_run(session, **ids_of(running), run_id=first).requested_by == "run"
        events = events_of_chat(
            session, **ids_of(running), chat_id=running.chat_id, after=None, limit=50
        )
    started = [
        event.detail
        for event in events
        if (event.run_kind, event.kind) == (TASK_RUN_KIND, RUN_STARTED)
    ]
    assert started == [{"run_kind": "studio-run", "run_id": str(first)}]
    assert len(task_database.task(running.run).steps) == 1


def test_a_writing_step_creates_its_run_with_the_asset_code_resolved(work, running, sessions):
    image = WriterDatabase(sessions).another_image(running.run)  # type: ignore[arg-type]
    brief = WritingBrief(
        platform=WritingPlatform.X,
        goal=WritingGoal.ANNOUNCE,
        feature="offline-sync",
        variants=1,
        asset=image.asset_id,
    )
    payload = RunWriting(task_id=running.run.run_id, step="post", brief=brief)

    child = work.prepare_writing(payload, context_of(running))

    with transaction(sessions) as session:
        made = run_to_execute(session, child)
    assert made is not None and made.brief.asset == image.asset_id


def test_a_writing_step_whose_asset_is_not_the_projects_is_refused_for_good(work, running):
    brief = WritingBrief(
        platform=WritingPlatform.X, goal=WritingGoal.INTRODUCE, variants=1, asset=uuid.uuid4()
    )
    payload = RunWriting(task_id=running.run.run_id, step="post", brief=brief)

    with pytest.raises(ApplicationError) as refused:
        work.prepare_writing(payload, context_of(running))

    assert (refused.value.type, refused.value.non_retryable) == (steps.STEP_NOT_STARTED, True)
    assert "step 'post'" in refused.value.message


def test_a_step_of_another_task_is_not_dispatched_by_this_one(work, running, task_database):
    other = task_database.requested()
    payload = RunResearch(task_id=other.run.run_id, step="research", request=RESEARCH)

    with pytest.raises(ApplicationError) as refused:
        work.prepare_research(payload, context_of(running))

    assert refused.value.type == steps.STEP_NOT_STARTED


def test_a_step_the_plan_does_not_hold_is_refused_and_its_run_left_undispatched(
    work, running, task_database
):
    payload = RunResearch(task_id=running.run.run_id, step="elsewhere", request=RESEARCH)

    with pytest.raises(ApplicationError) as refused:
        work.prepare_research(payload, context_of(running))

    assert refused.value.type == steps.STEP_NOT_STARTED
    assert task_database.task(running.run).steps == ()


# ------------------------------------------------------------------- how a step ended
@pytest.mark.parametrize(
    ("record", "outcome"),
    [
        (
            {"agent": "studio", "status": "succeeded", "outcome": {"needs_review": True}},
            TaskStepOutcome.NEEDS_REVIEW,
        ),
        (
            {"agent": "studio", "status": "succeeded", "outcome": {"needs_review": False}},
            TaskStepOutcome.SUCCEEDED,
        ),
        (
            {"agent": "writing", "status": "succeeded", "outcome": {"post_status": "rejected"}},
            TaskStepOutcome.SUCCEEDED,
        ),
        ({"agent": "research", "status": "failed", "outcome": {}}, TaskStepOutcome.FAILED),
        ({"agent": "research", "status": "pending", "outcome": {}}, None),
        ({"agent": "writing", "status": "dropped", "outcome": {}}, None),
    ],
)
def test_a_steps_run_outcome_is_read_from_its_status_and_a_studio_runs_review(record, outcome):
    assert step_run_outcome(record) == outcome


# ---------------------------------------------------------------------- a finished task
def a_final_state(asked, plan: TaskPlan, steps_ran: list[dict[str, Any]]) -> dict[str, Any]:
    request = TaskRequest(request="A post.", understanding="One post.")
    state = dict(
        initial_state(asked.run, request, limits=TaskLimits(), started_at=datetime.now(UTC))
    )
    changed = plan.model_copy(update={"goal": "Announce offline sync, with research."})
    state.update(
        plan=changed.model_dump(mode="json"),
        plan_history=[{"reason": "Research first.", "replaced": plan.model_dump(mode="json")}],
        plan_changes=1,
        steps=steps_ran,
        outcome="completed",
        report={"summary": "Goal: Announce offline sync.", "research_run_ids": []},
        cost_usd=0.0123456789,
        total_cost_usd=0.2,
    )
    return state


def test_finish_stores_the_plan_its_change_the_steps_outcomes_and_the_report(
    work, running, task_database
):
    child = work.prepare_research(
        RunResearch(task_id=running.run.run_id, step="research", request=RESEARCH),
        context_of(running),
    )
    ran = [
        {
            "id": "research",
            "agent": "research",
            "status": "succeeded",
            "run_id": str(child),
            "outcome": {"findings": 1},
            "error": "",
        },
        {
            "id": "image",
            "agent": "studio",
            "status": "dropped",
            "run_id": "",
            "outcome": {},
            "error": "the task ended before it",
        },
    ]
    final = a_final_state(running, the_plan(), ran)

    output = work.finish(None, final)  # type: ignore[arg-type]
    again = work.finish(None, final)  # type: ignore[arg-type]

    stored = task_database.task(running.run)
    assert (
        output
        == again
        == {
            "outcome": "completed",
            "cost_usd": "0.012346",
            "total_cost_usd": "0.200000",
        }
    )
    assert (stored.status, stored.outcome) == (TaskStatus.SUCCEEDED, TaskOutcome.COMPLETED)
    assert stored.plan is not None and stored.plan.goal == "Announce offline sync, with research."
    [change] = stored.plan_changes
    assert (change.reason, change.replaced) == ("Research first.", the_plan())
    assert [(step.step_key, step.outcome) for step in stored.steps] == [
        ("research", TaskStepOutcome.SUCCEEDED)
    ]
    assert stored.report is not None and stored.report.summary.startswith("Goal:")
    assert stored.cost_usd == Decimal("0.012346")


def test_finish_with_a_report_that_does_not_fit_ends_the_task_failed(work, running):
    final = a_final_state(running, the_plan(), [])
    final["report"] = {"summary": ""}

    with pytest.raises(ApplicationError) as refused:
        work.finish(None, final)  # type: ignore[arg-type]

    assert (refused.value.type, refused.value.message) == (
        steps.TASK_FAILED,
        steps.RESULT_UNREADABLE,
    )
