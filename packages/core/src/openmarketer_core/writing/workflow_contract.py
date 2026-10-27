"""What the API and the worker agree on to run a writing run as a durable workflow.

The API starts a workflow for a run it has stored (``db.writing_runs``); the worker executes
it. Entry points do not import each other, so the names both need are here: plain values,
with no import of the workflow engine. ``writing_workflow`` puts them together as the
``WorkflowStart`` the API's starter is given. Where the engine is, which both also need, is
not about writing: ``workflow_server.py``.

The input names the run and nothing else. The engine stores every input in its history,
where it can be read, so no brief, text or profile ever belongs in it; the worker reads the
run and its brief from the database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from openmarketer_core.workflow_start import WorkflowStart

WRITING_WORKFLOW = "WritePost"
WRITING_TASK_QUEUE = "openmarketer-writing"
# The kind of run a writing run is: what the writer's agent is declared with, and what the
# name of the thread of a run's checkpoints starts with (``db.writing_runs.run_thread_id``).
# It is here, with no import beside it, because the agent's declarations are loaded by the
# workflow and must not load the database with them.
WRITING_RUN_KIND = "writing-run"


@dataclass(frozen=True)
class WritingRunInput:
    """The run to execute: a run of ``db.writing_runs``, with its project and workspace."""

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID


def writing_workflow_id(run_id: uuid.UUID) -> str:
    """The workflow of a run. It is derived, never stored: one run, one workflow."""
    return f"writing-{run_id}"


def writing_workflow(run: WritingRunInput) -> WorkflowStart:
    """The workflow that executes ``run``, as the API's starter is asked to start it."""
    return WorkflowStart(
        workflow=WRITING_WORKFLOW,
        workflow_id=writing_workflow_id(run.run_id),
        task_queue=WRITING_TASK_QUEUE,
        input=run,
    )
