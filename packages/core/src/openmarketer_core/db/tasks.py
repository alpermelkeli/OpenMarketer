"""Orchestrator tasks: the state of each task of a project, its plan and the runs it started.

The chat's request action stores a task and starts its workflow; the worker's hooks report
that it started, the plan it carries out and each change of it, each step it dispatched with
the run that step started and how that run ended, and how the task itself ended. The rows are
the only shared record of that, so a task's state survives a restart of either.

A task moves forwards only:

- requested: ``queued``, with the operator's request and the chat's understanding of it;
- ``queued`` to ``running`` when the worker picks it up. Reporting the start again changes
  nothing, because a retried activity reports it again;
- while ``running``: its first plan is recorded once (``record_task_plan``; the same plan again
  changes nothing, another is ``TaskPlanRecorded``); each change of the plan replaces it and
  keeps the plan it replaced with the reason (``record_plan_change``; recording the plan in use
  again changes nothing); each step of the plan in use is dispatched once with the run it
  started (``record_step_dispatched``), and that run's outcome is recorded once
  (``record_step_outcome``);
- ``running`` to ``succeeded``, with the report, how the task came out (``TaskOutcome``: a
  task whose plan a person rejected ends here too, with its report) and what its own model
  calls cost. Reporting the same report and outcome again changes nothing;
- ``queued`` or ``running`` to ``failed``, with a message. Reporting a failure of a task that
  already failed changes nothing and keeps the first message.

Everything else is refused: a finished task is never started, failed, given another plan,
step or result (``TaskFinished``), and nothing but a failure is recorded for a task that was
never started (``TaskNotStarted``). Whether a plan keeps to its scope, how many changes it may
have, and which step may be dispatched when are the task agent's rules
(``orchestrator/task/task_agent/rules.py``), not this module's; it holds only that a step
dispatched is a step of the plan in use, with that step's agent, and that the run it names is
a run of that agent in the same project.

What is stored is read back strictly (``orchestrator/task/plan.py``): a request, plan, change
or report that no longer fits its schema is ``StoredTaskInvalid``.

Every function but one is scoped to a workspace and a project, and every write takes the
workspace lock, then holds the task's row: a run of another workspace or project is treated
exactly like a task that does not exist. The one is ``task_to_execute``, which is how the
worker's graph learns the scope of a task it was handed by its id, as each other kind of run
has its own. Functions take a session and never commit.

This module does not start or cancel workflows, and stores no workflow id: the caller derives
it from the task's id (``orchestrator/task/workflow_contract.py``). The thread a task's graph
checkpoints are kept under is derived from the id too (``run_thread_id``). Tasks are deleted
with their project (``db/project_removal.py``); deleting one task on its own is not built.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import Select, func, select, tuple_, update
from sqlalchemy.orm import Session

from openmarketer_core.agent_runtime.threads import thread_id
from openmarketer_core.db.chat import require_chat
from openmarketer_core.db.models import (
    RUN_ERROR_MAX_LENGTH,
    Project,
    ResearchRunRecord,
    StudioRunRecord,
    TaskRecord,
    TaskStatus,
    TaskStepOutcome,
    TaskStepRecord,
    WritingRunRecord,
)
from openmarketer_core.db.pages import CreatedPosition, Page, page_of, rows_to_fetch
from openmarketer_core.db.projects import get_project
from openmarketer_core.db.research_runs import ResearchRunNotFound
from openmarketer_core.db.studio_runs import StudioRunNotFound
from openmarketer_core.db.workspace_lock import wait_for_other_writes
from openmarketer_core.db.writing_runs import WritingRunNotFound
from openmarketer_core.orchestrator.task.plan import (
    StoredTaskInvalid,
    TaskAgentName,
    TaskOutcome,
    TaskPlan,
    TaskPlanChange,
    TaskReport,
    TaskRequest,
)
from openmarketer_core.orchestrator.task.workflow_contract import TASK_RUN_KIND

FINISHED = (TaskStatus.SUCCEEDED, TaskStatus.FAILED)

# What the name of every thread of a task starts with; see ``db/research_runs.py`` for why a
# kind and a colon.
TASK_RUN_THREADS = f"{TASK_RUN_KIND}:"

# The table of each kind of run a step can start.
_CHILD_RUNS = {
    TaskAgentName.RESEARCH: ResearchRunRecord,
    TaskAgentName.WRITING: WritingRunRecord,
    TaskAgentName.STUDIO: StudioRunRecord,
}
_CHILD_RUN_NOT_FOUND = {
    TaskAgentName.RESEARCH: ResearchRunNotFound,
    TaskAgentName.WRITING: WritingRunNotFound,
    TaskAgentName.STUDIO: StudioRunNotFound,
}


def run_thread_id(task_id: uuid.UUID) -> str:
    """The thread a task keeps its checkpoints under: its kind, then the task's id.

    ``db/checkpoint_cleanup.py`` finds the threads of tasks, and the task of each, by the same
    prefix.
    """
    return thread_id(TASK_RUN_KIND, task_id)


class TaskNotFound(Exception):
    """The workspace has no such task of such a project."""

    def __init__(self, project_id: uuid.UUID, task_id: uuid.UUID) -> None:
        super().__init__(f"project {project_id} has no task {task_id}")


class TaskFinished(Exception):
    """The task ended before; its outcome is recorded once and nothing is added to it."""

    def __init__(self, task_id: uuid.UUID, status: TaskStatus) -> None:
        self.status = status
        super().__init__(f"task {task_id} has already ended: {status.value}")


class TaskNotStarted(Exception):
    """The task is still queued; what is asked needs a task that was started."""

    def __init__(self, task_id: uuid.UUID) -> None:
        super().__init__(f"task {task_id} was never started")


class TaskPlanRecorded(Exception):
    """The task has another first plan, recorded before; a later plan is a change of it."""

    def __init__(self, task_id: uuid.UUID) -> None:
        super().__init__(f"task {task_id} has a plan already; a new one is a change of it")


class TaskHasNoPlan(Exception):
    """The task has no plan yet; it is changed, and its steps dispatched, once it has one."""

    def __init__(self, task_id: uuid.UUID) -> None:
        super().__init__(f"task {task_id} has no plan yet")


class TaskStepNotInPlan(Exception):
    """The plan in use has no step of that id and agent."""

    def __init__(self, task_id: uuid.UUID, step_key: str) -> None:
        super().__init__(f"the plan of task {task_id} has no such step {step_key!r}")


class TaskStepDispatched(Exception):
    """The step was dispatched before with another run; a step starts one run."""

    def __init__(self, task_id: uuid.UUID, step_key: str) -> None:
        super().__init__(f"step {step_key!r} of task {task_id} started another run")


class TaskStepNotDispatched(Exception):
    """The step started no run yet; only a dispatched step has an outcome."""

    def __init__(self, task_id: uuid.UUID, step_key: str) -> None:
        super().__init__(f"step {step_key!r} of task {task_id} was never dispatched")


class TaskStepOutcomeRecorded(Exception):
    """The step's run ended before with another outcome; it is recorded once."""

    def __init__(self, task_id: uuid.UUID, step_key: str) -> None:
        super().__init__(f"step {step_key!r} of task {task_id} has another outcome")


@dataclass(frozen=True)
class TaskStepRun:
    """A dispatched step: its id in the plan, its agent, the run it started and its outcome."""

    step_key: str
    agent: TaskAgentName
    child_run_id: uuid.UUID
    outcome: TaskStepOutcome | None
    created_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class OrchestratorTask:
    """One task and how far it got, with its plan, its changes and its dispatched steps.

    ``plan`` is the plan in use, ``None`` until the first is recorded; ``plan_changes`` the
    changes since, oldest first; ``steps`` the dispatched steps, in the order they were
    dispatched; ``report`` what a succeeded task ended with and ``outcome`` how it came out,
    both ``None`` until then. ``cost_usd`` is what the task's
    own model calls cost. ``chat_thread_id`` is the chat whose turn started the task, ``None``
    for a task started anywhere else or whose chat was deleted.
    """

    id: uuid.UUID
    project_id: uuid.UUID
    status: TaskStatus
    error: str | None
    request: TaskRequest
    plan: TaskPlan | None
    plan_changes: tuple[TaskPlanChange, ...]
    steps: tuple[TaskStepRun, ...]
    report: TaskReport | None
    outcome: TaskOutcome | None
    cost_usd: Decimal
    requested_by: str
    chat_thread_id: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class TaskSummary:
    """A task as a list shows it: its status, outcome and goal, without plan or report."""

    id: uuid.UUID
    project_id: uuid.UUID
    status: TaskStatus
    outcome: TaskOutcome | None
    error: str | None
    goal: str | None
    cost_usd: Decimal
    requested_by: str
    chat_thread_id: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class TaskToExecute:
    """The task ``task_to_execute`` found: its scope, its request, who asked, and when.

    ``created_at`` is when the task was requested, the same at every attempt: what its
    deadline counts from.
    """

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    request: TaskRequest
    requested_by: str
    chat_thread_id: uuid.UUID | None
    created_at: datetime


def request_task_run(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    request: TaskRequest,
    requested_by: str,
    chat_thread_id: uuid.UUID | None = None,
) -> OrchestratorTask:
    """Add a queued task for the project, with the request it carries out.

    ``chat_thread_id`` is the chat whose turn asked for the task, a chat of the same project.
    Raises ``ProjectNotFound`` when the workspace has no such project, and ``ChatNotFound``
    when the project has no such chat.
    """
    wait_for_other_writes(session, workspace_id)
    project = get_project(session, workspace_id=workspace_id, project_id=project_id)
    if chat_thread_id is not None:
        require_chat(
            session, workspace_id=workspace_id, project_id=project_id, chat_id=chat_thread_id
        )
    record = TaskRecord(
        project_id=project.id,
        requested_by=requested_by,
        chat_thread_id=chat_thread_id,
        request=request.request,
        understanding=request.understanding,
    )
    session.add(record)
    session.flush()
    session.refresh(record)
    return _task(session, record)


def mark_task_started(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, task_id: uuid.UUID
) -> OrchestratorTask:
    """Record that a worker picked the task up. Safe to repeat while it is running."""
    record = _task_to_change(session, workspace_id, project_id, task_id)
    if record.status in FINISHED:
        raise TaskFinished(task_id, record.status)
    if record.status is TaskStatus.QUEUED:
        _change(session, record, status=TaskStatus.RUNNING, started_at=func.now())
    return _task(session, record)


def record_task_plan(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    plan: TaskPlan,
) -> OrchestratorTask:
    """Record the task's first plan. The same plan again changes nothing.

    Raises ``TaskPlanRecorded`` when the task has another plan already.
    """
    record = _running_task(session, workspace_id, project_id, task_id)
    if record.plan is None:
        _change(session, record, plan=plan.model_dump(mode="json"))
    elif _stored_plan(record) != plan:
        raise TaskPlanRecorded(task_id)
    return _task(session, record)


def record_plan_change(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    plan: TaskPlan,
    reason: str,
) -> OrchestratorTask:
    """Replace the task's plan with ``plan``, keeping the plan it replaces and ``reason``.

    Recording the plan in use again changes nothing, so a retried attempt adds no second
    change. ``reason`` is read as ``TaskPlanChange`` reads it (a ``ValidationError`` when it
    does not fit). Raises ``TaskHasNoPlan`` before the first plan is recorded.
    """
    record = _running_task(session, workspace_id, project_id, task_id)
    if record.plan is None:
        raise TaskHasNoPlan(task_id)
    in_use = _stored_plan(record)
    if in_use == plan:
        return _task(session, record)
    change = TaskPlanChange(reason=reason, replaced=in_use)
    _change(
        session,
        record,
        plan=plan.model_dump(mode="json"),
        plan_changes=[*record.plan_changes, change.model_dump(mode="json")],
    )
    return _task(session, record)


def record_step_dispatched(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    step_key: str,
    agent: TaskAgentName,
    child_run_id: uuid.UUID,
) -> OrchestratorTask:
    """Record that step ``step_key`` of the plan in use started the run ``child_run_id``.

    The step must be a step of the plan in use with the agent ``agent`` (``TaskHasNoPlan``,
    ``TaskStepNotInPlan``), and the run a run of that agent in the task's project
    (``ResearchRunNotFound``, ``WritingRunNotFound`` or ``StudioRunNotFound``). The same run
    again changes nothing; another is ``TaskStepDispatched``.
    """
    record = _running_task(session, workspace_id, project_id, task_id)
    if record.plan is None:
        raise TaskHasNoPlan(task_id)
    if not any(s.id == step_key and s.agent == agent for s in _stored_plan(record).steps):
        raise TaskStepNotInPlan(task_id, step_key)
    dispatched = _step(session, project_id, task_id, step_key)
    if dispatched is not None:
        if dispatched.child_run_id != child_run_id:
            raise TaskStepDispatched(task_id, step_key)
        return _task(session, record)
    _require_child_run(session, project_id, agent, child_run_id)
    session.add(
        TaskStepRecord(
            project_id=project_id,
            task_id=task_id,
            step_key=step_key,
            agent=agent.value,
            child_run_id=child_run_id,
            # The clock of the statement, not of the transaction: two steps dispatched in one
            # transaction still read back in the order they were dispatched.
            created_at=func.clock_timestamp(),
        )
    )
    session.flush()
    return _task(session, record)


def record_step_outcome(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    step_key: str,
    outcome: TaskStepOutcome,
) -> OrchestratorTask:
    """Record how the run of the dispatched step ``step_key`` ended.

    The same outcome again changes nothing; another is ``TaskStepOutcomeRecorded``. Raises
    ``TaskStepNotDispatched`` for a step that started no run, and ``ValueError`` for
    ``needs_review`` of a step that is not the studio's.
    """
    record = _running_task(session, workspace_id, project_id, task_id)
    dispatched = _step(session, project_id, task_id, step_key)
    if dispatched is None:
        raise TaskStepNotDispatched(task_id, step_key)
    if dispatched.outcome is not None:
        if dispatched.outcome is not outcome:
            raise TaskStepOutcomeRecorded(task_id, step_key)
        return _task(session, record)
    if outcome is TaskStepOutcome.NEEDS_REVIEW and dispatched.agent != TaskAgentName.STUDIO:
        raise ValueError("only a studio run ends needing review")
    session.execute(
        update(TaskStepRecord)
        .where(TaskStepRecord.id == dispatched.id)
        .values(outcome=outcome, finished_at=func.now())
    )
    session.refresh(dispatched)
    return _task(session, record)


def mark_task_succeeded(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    outcome: TaskOutcome,
    report: TaskReport,
    cost_usd: Decimal,
) -> OrchestratorTask:
    """Record the task's report, how it came out, what its own model calls cost, and that it
    is finished.

    The same report and outcome again change nothing. Raises ``TaskNotStarted`` for a queued task,
    ``TaskHasNoPlan`` for a task with no plan, and ``TaskFinished`` for one that failed or
    ended with another report or outcome.
    """
    record = _task_to_change(session, workspace_id, project_id, task_id)
    if record.status is TaskStatus.QUEUED:
        raise TaskNotStarted(task_id)
    ended_alike = record.outcome is outcome and _stored_report(record) == report
    if record.status is TaskStatus.SUCCEEDED and ended_alike:
        return _task(session, record)
    if record.status in FINISHED:
        raise TaskFinished(task_id, record.status)
    if record.plan is None:
        raise TaskHasNoPlan(task_id)
    _change(
        session,
        record,
        status=TaskStatus.SUCCEEDED,
        outcome=outcome,
        report=report.model_dump(mode="json"),
        cost_usd=cost_usd,
        finished_at=func.now(),
    )
    return _task(session, record)


def mark_task_failed(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    error: str,
    cost_usd: Decimal = Decimal(0),
) -> OrchestratorTask:
    """Record that the task ended without a report, keeping the start of ``error``.

    ``cost_usd`` is what its own model calls spent before it failed. The message is shortened
    here rather than refused, so that a failure can always be recorded. The runs it started are
    not changed: each ends on its own.
    """
    record = _task_to_change(session, workspace_id, project_id, task_id)
    if record.status is TaskStatus.SUCCEEDED:
        raise TaskFinished(task_id, record.status)
    if record.status is not TaskStatus.FAILED:
        _change(
            session,
            record,
            status=TaskStatus.FAILED,
            error=error[:RUN_ERROR_MAX_LENGTH],
            cost_usd=cost_usd,
            finished_at=func.now(),
        )
    return _task(session, record)


def orchestrator_task(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, task_id: uuid.UUID
) -> OrchestratorTask:
    """The task as it is now, with its plan, its changes, its steps and its report."""
    record = session.scalar(_task_of_project(workspace_id, project_id, task_id))
    if record is None:
        raise TaskNotFound(project_id, task_id)
    return _task(session, record)


def task_to_execute(session: Session, task_id: uuid.UUID) -> TaskToExecute | None:
    """The task ``task_id`` with its project, workspace, request, asker and chat, or ``None``.

    This one read is not scoped by a workspace, because it is what finds the scope: the
    worker's graph is handed a task by its id alone (the id this module made and the caller
    gave the task's workflow, never a value a request or a model chose), and everything it then
    does with the task goes through the scoped functions with what this returns. It says
    nothing else of the task and changes nothing.
    """
    found = session.execute(
        select(Project.workspace_id, TaskRecord)
        .join(Project, Project.id == TaskRecord.project_id)
        .where(TaskRecord.id == task_id)
    ).one_or_none()
    if found is None:
        return None
    workspace_id, record = found
    return TaskToExecute(
        workspace_id=workspace_id,
        project_id=record.project_id,
        run_id=record.id,
        request=_stored_request(record),
        requested_by=record.requested_by,
        chat_thread_id=record.chat_thread_id,
        created_at=record.created_at,
    )


def list_tasks(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    limit: int,
    before: CreatedPosition | None = None,
) -> Page[TaskSummary, CreatedPosition]:
    """The project's tasks as they are now, newest first, each with its plan's goal.

    At most ``limit`` of them, starting after ``before`` (the ``next_before`` of the previous
    page) or at the newest. Neither a plan nor a report is read. Raises ``ProjectNotFound``
    when the workspace has no such project.
    """
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    query = (
        select(
            TaskRecord.id,
            TaskRecord.project_id,
            TaskRecord.status,
            TaskRecord.outcome,
            TaskRecord.error,
            TaskRecord.plan["goal"].astext,
            TaskRecord.cost_usd,
            TaskRecord.requested_by,
            TaskRecord.chat_thread_id,
            TaskRecord.created_at,
            TaskRecord.started_at,
            TaskRecord.finished_at,
        )
        .join(Project, Project.id == TaskRecord.project_id)
        .where(Project.workspace_id == workspace_id, Project.id == project_id)
    )
    if before is not None:
        query = query.where(
            tuple_(TaskRecord.created_at, TaskRecord.id) < tuple_(before.created_at, before.id)
        )
    rows = session.execute(
        query.order_by(TaskRecord.created_at.desc(), TaskRecord.id.desc()).limit(
            rows_to_fetch(limit)
        )
    )
    tasks = [TaskSummary(*row) for row in rows]
    return page_of(tasks, limit, lambda last: CreatedPosition(last.created_at, last.id))


def _require_child_run(
    session: Session, project_id: uuid.UUID, agent: TaskAgentName, run_id: uuid.UUID
) -> None:
    runs = _CHILD_RUNS[agent]
    found = session.scalar(select(runs.id).where(runs.id == run_id, runs.project_id == project_id))
    if found is None:
        raise _CHILD_RUN_NOT_FOUND[agent](project_id, run_id)


def _step(
    session: Session, project_id: uuid.UUID, task_id: uuid.UUID, step_key: str
) -> TaskStepRecord | None:
    return session.scalar(
        select(TaskStepRecord)
        .where(
            TaskStepRecord.project_id == project_id,
            TaskStepRecord.task_id == task_id,
            TaskStepRecord.step_key == step_key,
        )
        .execution_options(populate_existing=True)
    )


def _task_of_project(
    workspace_id: uuid.UUID, project_id: uuid.UUID, task_id: uuid.UUID
) -> Select[TaskRecord]:
    return (
        select(TaskRecord)
        .join(Project, Project.id == TaskRecord.project_id)
        .where(
            Project.workspace_id == workspace_id,
            Project.id == project_id,
            TaskRecord.id == task_id,
        )
    )


def _task_to_change(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID, task_id: uuid.UUID
) -> TaskRecord:
    """The task, after the workspace lock, with its row held until the transaction ends.

    Two reports about one task then apply one after the other, and the second sees what the
    first recorded.
    """
    wait_for_other_writes(session, workspace_id)
    record = session.scalar(
        _task_of_project(workspace_id, project_id, task_id)
        .with_for_update(of=TaskRecord)
        # A row the session still holds from before it waited may have changed since.
        .execution_options(populate_existing=True)
    )
    if record is None:
        raise TaskNotFound(project_id, task_id)
    return record


def _running_task(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID, task_id: uuid.UUID
) -> TaskRecord:
    record = _task_to_change(session, workspace_id, project_id, task_id)
    if record.status in FINISHED:
        raise TaskFinished(task_id, record.status)
    if record.status is TaskStatus.QUEUED:
        raise TaskNotStarted(task_id)
    return record


def _change(session: Session, record: TaskRecord, **columns: object) -> None:
    session.execute(update(TaskRecord).where(TaskRecord.id == record.id).values(**columns))
    session.refresh(record)


def _stored_request(record: TaskRecord) -> TaskRequest:
    try:
        return TaskRequest(request=record.request, understanding=record.understanding)
    except ValidationError as e:
        raise StoredTaskInvalid(record.id) from e


def _stored_plan(record: TaskRecord) -> TaskPlan:
    try:
        return TaskPlan.model_validate(record.plan)
    except ValidationError as e:
        raise StoredTaskInvalid(record.id) from e


def _stored_report(record: TaskRecord) -> TaskReport | None:
    if record.report is None:
        return None
    try:
        return TaskReport.model_validate(record.report)
    except ValidationError as e:
        raise StoredTaskInvalid(record.id) from e


def _stored_changes(record: TaskRecord) -> tuple[TaskPlanChange, ...]:
    try:
        return tuple(TaskPlanChange.model_validate(change) for change in record.plan_changes)
    except ValidationError as e:
        raise StoredTaskInvalid(record.id) from e


def _task(session: Session, record: TaskRecord) -> OrchestratorTask:
    steps = session.scalars(
        select(TaskStepRecord)
        .where(TaskStepRecord.project_id == record.project_id, TaskStepRecord.task_id == record.id)
        .order_by(TaskStepRecord.created_at, TaskStepRecord.id)
    )
    return OrchestratorTask(
        id=record.id,
        project_id=record.project_id,
        status=record.status,
        error=record.error,
        request=_stored_request(record),
        plan=None if record.plan is None else _stored_plan(record),
        plan_changes=_stored_changes(record),
        steps=tuple(
            TaskStepRun(
                step_key=step.step_key,
                agent=TaskAgentName(step.agent),
                child_run_id=step.child_run_id,
                outcome=step.outcome,
                created_at=step.created_at,
                finished_at=step.finished_at,
            )
            for step in steps
        ),
        report=_stored_report(record),
        outcome=record.outcome,
        cost_usd=record.cost_usd,
        requested_by=record.requested_by,
        chat_thread_id=record.chat_thread_id,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )
