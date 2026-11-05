"""Requesting an orchestrator task: store the task, then hand it to the workflow engine.

The order is the point, as it is for every kind of run. The task is committed before its
workflow is started, so the worker finds the row it is asked to execute. If the workflow
cannot be started, the task is marked failed at once: nobody would ever execute this one.

A project may have several tasks at once; each starts runs of its own and keeps to its own
scope.

Which workflow executes the task is the contract's (``task_workflow``); how a workflow is
started is the caller's business, and it passes a function. This module does not wait for the
task and does not read its status.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.session import transaction
from openmarketer_core.db.tasks import OrchestratorTask, mark_task_failed, request_task_run
from openmarketer_core.orchestrator.task.plan import TaskRequest
from openmarketer_core.orchestrator.task.workflow_contract import TaskRunInput, task_workflow
from openmarketer_core.workflow_start import WorkflowNotStarted, WorkflowStart

# Who asked for a task: a person through the API, or the orchestrator's chat. The task's record
# keeps one of them (the column's CHECK lists these and ``run``, which no task uses yet).
REQUESTED_BY_A_PERSON = "person"
REQUESTED_BY_THE_CHAT = "orchestrator"
WORKFLOW_NOT_STARTED = "the task could not be handed to a worker"


class TaskWorkflowNotStarted(Exception):
    """The task was stored, but no workflow could be started for it; it is now failed."""

    def __init__(self, task_id: uuid.UUID) -> None:
        self.task_id = task_id
        super().__init__(
            f"task {task_id} could not be handed to a worker and is recorded as failed; "
            "start a new one when the workflow server is reachable"
        )


def request_task(
    sessions: sessionmaker[Session],
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    request: TaskRequest,
    start_workflow: Callable[[WorkflowStart], None],
    requested_by: str = REQUESTED_BY_THE_CHAT,
    chat_thread_id: uuid.UUID | None = None,
) -> OrchestratorTask:
    """Queue a task for the project and start its workflow.

    ``chat_thread_id`` is the chat whose turn asks for the task, which hears back when it
    ends. Raises ``ProjectNotFound`` when the workspace has no such project, ``ChatNotFound``
    when the project has no such chat, and ``TaskWorkflowNotStarted`` when the workflow could
    not be started.
    """
    with transaction(sessions) as session:
        task = request_task_run(
            session,
            workspace_id=workspace_id,
            project_id=project_id,
            request=request,
            requested_by=requested_by,
            chat_thread_id=chat_thread_id,
        )
    try:
        start_workflow(
            task_workflow(
                TaskRunInput(workspace_id=workspace_id, project_id=project_id, run_id=task.id)
            )
        )
    except WorkflowNotStarted as e:
        with transaction(sessions) as session:
            mark_task_failed(
                session,
                workspace_id=workspace_id,
                project_id=project_id,
                task_id=task.id,
                error=WORKFLOW_NOT_STARTED,
            )
        raise TaskWorkflowNotStarted(task.id) from e
    return task
