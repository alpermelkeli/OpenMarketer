"""What the API and the worker agree on to run an orchestrator task as a durable workflow.

The chat's request action stores a task (``db.tasks``) and starts its workflow; the worker
executes it. Entry points do not import each other, so the names both need are here: plain
values, with no import of the workflow engine. ``task_workflow`` puts them together as the
``WorkflowStart`` the starter is given. Where the engine is, which both also need, is not about
tasks: ``workflow_server.py``.

The input names the task and nothing else. The engine stores every input in its history,
where it can be read, so no request, plan or report ever belongs in it; the worker reads the
task and its request from the database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from openmarketer_core.workflow_start import WorkflowStart

TASK_WORKFLOW = "OrchestratorTask"
TASK_TASK_QUEUE = "openmarketer-task"
# The kind of run a task is: what the task's agent is declared with, what the name of the
# thread of a task's checkpoints starts with (``db.tasks.run_thread_id``), and the ``run_kind``
# of the chat's follow-up on a task. It is here, with no import beside it, because the agent's
# declarations are loaded by the workflow and must not load the database with them.
TASK_RUN_KIND = "orchestrator-task"


@dataclass(frozen=True)
class TaskRunInput:
    """The task to execute: a task of ``db.tasks``, with its project and workspace."""

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID


def task_workflow_id(run_id: uuid.UUID) -> str:
    """The workflow of a task. It is derived, never stored: one task, one workflow."""
    return f"orchestrator-task-{run_id}"


def task_workflow(run: TaskRunInput) -> WorkflowStart:
    """The workflow that executes ``run``, as the starter is asked to start it."""
    return WorkflowStart(
        workflow=TASK_WORKFLOW,
        workflow_id=task_workflow_id(run.run_id),
        task_queue=TASK_TASK_QUEUE,
        input=run,
    )
