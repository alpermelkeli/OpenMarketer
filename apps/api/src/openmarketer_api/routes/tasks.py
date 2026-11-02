"""The orchestrator's tasks: list a project's tasks and read one as it is now.

A task is started by the chat's request action (``start_task``), never by a route, and the
worker executes it; its state, its plan, the steps it dispatched with the runs they started
and its report are read from the database only, and no route asks the workflow engine about a
task. A plan a task holds for a person to confirm is decided on its proposal
(``routes/proposals.py``); nothing here confirms, rejects, changes or cancels a task.

A single task is read with its request, its plan, the plans its changes replaced, its steps
and its report, each read back strictly by core's schemas; the list leaves all of them out
but the plan's goal. A step names the run it started by that run's id and agent, so the
dashboard links it to the research run, studio run or writing run it is. Tasks are deleted
with their project.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from openmarketer_api.dependencies import DbSession
from openmarketer_api.errors import problems
from openmarketer_api.identity import CurrentWorkspaceId
from openmarketer_api.pagination import DEFAULT_PAGE_SIZE, CreatedBefore, PageLimit, created_cursor
from openmarketer_core.db.models import TaskStatus, TaskStepOutcome
from openmarketer_core.db.tasks import (
    OrchestratorTask,
    TaskStepRun,
    TaskSummary,
    list_tasks,
    orchestrator_task,
)
from openmarketer_core.orchestrator.task.plan import (
    TaskAgentName,
    TaskOutcome,
    TaskPlan,
    TaskPlanChange,
    TaskReport,
    TaskRequest,
)

router = APIRouter(prefix="/projects/{project_id}/tasks", tags=["tasks"])


class TaskResponse(BaseModel):
    """A task as a list shows it: its state and its plan's goal."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    project_id: uuid.UUID
    status: TaskStatus = Field(
        description=(
            "`queued`, `running` (planning, waiting for its plan to be confirmed or for a "
            "run it started), `succeeded` once it ended with its report, or `failed`."
        )
    )
    outcome: TaskOutcome | None = Field(
        description="How a succeeded task came out; null until it has ended with its report."
    )
    goal: str | None = Field(description="The goal of the plan in use; null before it has one.")
    error: str | None
    requested_by: str = Field(
        description="Who asked for the task: `orchestrator` (the chat) or `person`."
    )
    chat_thread_id: uuid.UUID | None = Field(
        description="The chat whose turn started the task; null when its chat was deleted."
    )
    cost_usd: float = Field(
        description="What the task's own model calls cost, in USD; each run it started has its own."
    )
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TaskStepResponse(BaseModel):
    """A step of the plan that was dispatched: the run it started and how that run ended."""

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(description="The step's id in the plan, such as `image`.")
    agent: TaskAgentName
    run_id: uuid.UUID = Field(
        description=(
            "The run the step started: a research run, a studio run or a writing run, "
            "as `agent` says."
        )
    )
    outcome: TaskStepOutcome | None = Field(
        description="How the run ended; null while it is queued, running or waiting."
    )
    created_at: datetime
    finished_at: datetime | None


class TaskDetailResponse(TaskResponse):
    """A task with what it was asked, its plan and its changes, its steps and its report."""

    request: TaskRequest = Field(
        description="The operator's request in their words and the chat's understanding of it."
    )
    plan: TaskPlan | None = Field(description="The plan in use; null before the first one.")
    plan_changes: list[TaskPlanChange] = Field(
        description="Each change of the plan, oldest first, with the plan it replaced."
    )
    steps: list[TaskStepResponse] = Field(
        description="The steps dispatched so far, in the order they were dispatched."
    )
    report: TaskReport | None = Field(
        description="What the task did, as code wrote it; null until it has ended with it."
    )


class TaskListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[TaskResponse]
    next_cursor: str | None = Field(
        description="Null on the last page; otherwise the `cursor` of the next request."
    )


def _summary_response(task: TaskSummary) -> TaskResponse:
    return TaskResponse(
        id=task.id,
        project_id=task.project_id,
        status=task.status,
        outcome=task.outcome,
        goal=task.goal,
        error=task.error,
        requested_by=task.requested_by,
        chat_thread_id=task.chat_thread_id,
        cost_usd=float(task.cost_usd),
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
    )


def _step_response(step: TaskStepRun) -> TaskStepResponse:
    return TaskStepResponse(
        step_id=step.step_key,
        agent=step.agent,
        run_id=step.child_run_id,
        outcome=step.outcome,
        created_at=step.created_at,
        finished_at=step.finished_at,
    )


def _detail_response(task: OrchestratorTask) -> TaskDetailResponse:
    return TaskDetailResponse(
        id=task.id,
        project_id=task.project_id,
        status=task.status,
        outcome=task.outcome,
        goal=None if task.plan is None else task.plan.goal,
        error=task.error,
        requested_by=task.requested_by,
        chat_thread_id=task.chat_thread_id,
        cost_usd=float(task.cost_usd),
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
        request=task.request,
        plan=task.plan,
        plan_changes=list(task.plan_changes),
        steps=[_step_response(step) for step in task.steps],
        report=task.report,
    )


@router.get(
    "",
    operation_id="listTasks",
    response_model=TaskListResponse,
    responses=problems(404),
)
def list_project_tasks(
    project_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    before: CreatedBefore,
    limit: PageLimit = DEFAULT_PAGE_SIZE,
) -> TaskListResponse:
    """The project's tasks as they are now, newest first, one page at a time.

    Each with its plan's goal, without its request, plan, steps or report.
    """
    page = list_tasks(
        session, workspace_id=workspace_id, project_id=project_id, limit=limit, before=before
    )
    return TaskListResponse(
        tasks=[_summary_response(task) for task in page.items],
        next_cursor=created_cursor(page.next_before),
    )


@router.get(
    "/{task_id}",
    operation_id="getTask",
    response_model=TaskDetailResponse,
    responses=problems(404),
)
def get_task(
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
) -> TaskDetailResponse:
    """The task as it is now, with its request, plan, plan changes, steps and report.

    404 `task_not_found` when the project has no such task.
    """
    task = orchestrator_task(
        session, workspace_id=workspace_id, project_id=project_id, task_id=task_id
    )
    return _detail_response(task)
