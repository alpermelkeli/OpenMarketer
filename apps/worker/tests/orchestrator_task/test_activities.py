"""Tests for the task's activities as plain functions: its start, its end, its failure, and what
a step that waits keeps of the plan. Against PostgreSQL; skipped when it is not there."""

import json
from decimal import Decimal

import pytest
from task_fakes import ScriptedOrchestrator, a_post_plan, a_review, text
from temporalio.exceptions import ApplicationError

from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.db.models import TaskStatus
from openmarketer_core.db.session import transaction
from openmarketer_core.db.tasks import mark_task_failed
from openmarketer_core.orchestrator.task.task_agent.definitions import RUN_WRITING
from openmarketer_core.orchestrator.task.workflow_contract import TASK_RUN_KIND
from openmarketer_core.run_events import FAILED, FINISHED
from openmarketer_worker.agent_runtime.vocabulary import RUN_GRAPH_STEP, StepInput
from openmarketer_worker.orchestrator_task import steps
from openmarketer_worker.orchestrator_task.policy import TaskPolicy


@pytest.fixture
def activities(task_registration, activities_of):
    return activities_of(task_registration())


def test_starting_a_task_marks_it_running_and_gives_this_workers_limits(activities, task_database):
    asked = task_database.requested()

    configured = activities.run(steps.START_TASK, asked.run)

    assert configured == TaskPolicy(
        attempt_timeout_seconds=60,
        heartbeat_timeout_seconds=10,
        max_attempts=2,
        retry_after_seconds=1,
    )
    assert task_database.task(asked.run).status is TaskStatus.RUNNING


def test_a_task_that_ended_is_not_started_again(activities, task_database, sessions):
    asked = task_database.requested()
    with transaction(sessions) as session:
        mark_task_failed(
            session,
            workspace_id=asked.run.workspace_id,
            project_id=asked.run.project_id,
            task_id=asked.run.run_id,
            error="no",
        )

    with pytest.raises(ApplicationError) as refused:
        activities.run(steps.START_TASK, asked.run)

    assert (refused.value.type, refused.value.non_retryable) == (steps.TASK_UNAVAILABLE, True)


def test_the_end_of_a_task_is_its_last_event_and_names_the_chat_that_asked(
    activities, task_database
):
    asked = task_database.requested()

    told = activities.run(steps.RECORD_END, steps.TaskEnd(run=asked.run, outcome="completed"))

    assert told == steps.TaskEndRecorded(chat_id=asked.chat_id)
    assert task_database.event_kinds(asked)[-1] == FINISHED


def test_a_task_asked_for_elsewhere_has_no_chat_to_tell(activities, task_database):
    asked = task_database.requested(from_a_chat=False)

    told = activities.run(steps.RECORD_END, steps.TaskEnd(run=asked.run, outcome="completed"))

    assert told == steps.TaskEndRecorded(chat_id=None)


async def test_a_step_that_waits_for_a_run_has_its_plan_stored_before_the_run_is_made(
    activities, task_database
):
    asked = task_database.requested()
    activities.run(steps.START_TASK, asked.run)

    step = await activities.run_async(
        RUN_GRAPH_STEP, StepInput(run=GraphRunRef(TASK_RUN_KIND, asked.run.run_id), resume=None)
    )

    assert step.action is not None and step.action.name == RUN_WRITING
    stored = task_database.task(asked.run)
    assert stored.plan is not None and [s.id for s in stored.plan.steps] == ["post"]
    assert stored.steps == ()  # dispatched only once its run is made


async def test_a_failed_task_is_recorded_with_what_its_model_calls_cost_and_tells_its_chat(
    task_registration, activities_of, task_database
):
    activities = activities_of(
        task_registration(
            model=ScriptedOrchestrator(
                text(json.loads(a_post_plan().text), cost_usd=0.25), a_review()
            )
        )
    )
    asked = task_database.requested()
    activities.run(steps.START_TASK, asked.run)
    await activities.run_async(
        RUN_GRAPH_STEP, StepInput(run=GraphRunRef(TASK_RUN_KIND, asked.run.run_id), resume=None)
    )

    told = activities.run(
        steps.RECORD_FAILURE, steps.TaskEnd(run=asked.run, reason="the task was cancelled")
    )

    stored = task_database.task(asked.run)
    assert told == steps.TaskEndRecorded(chat_id=asked.chat_id)
    assert (stored.status, stored.error) == (TaskStatus.FAILED, "the task was cancelled")
    assert stored.cost_usd == Decimal("0.25")
    assert task_database.event_kinds(asked)[-1] == FAILED


def test_a_task_that_failed_before_any_step_is_recorded_as_having_cost_nothing(
    activities, task_database
):
    asked = task_database.requested()

    activities.run(steps.RECORD_FAILURE, steps.TaskEnd(run=asked.run, reason="no worker"))

    assert task_database.task(asked.run).cost_usd == Decimal(0)
