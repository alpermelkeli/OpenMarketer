"""What the API and the worker agree on to run a studio run as a durable workflow.

The API starts a workflow for a run it has stored (``db.studio_runs``); the worker executes
it. Entry points do not import each other, so the names both need are here: plain values,
with no import of the workflow engine. ``studio_workflow`` puts them together as the
``WorkflowStart`` the API's starter is given. Where the engine is, which both also need, is
not about the studio: ``workflow_server.py``.

The input names the run and nothing else. The engine stores every input in its history,
where it can be read, so no goal, copy or image ever belongs in it; the worker reads the run
and its request from the database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from openmarketer_core.workflow_start import WorkflowStart

STUDIO_WORKFLOW = "CreativeStudio"
STUDIO_TASK_QUEUE = "openmarketer-studio"
# The kind of run a studio run is: what the studio's agent is declared with, and what the
# name of the thread of a run's checkpoints starts with (``db.studio_runs.run_thread_id``).
# It is here, with no import beside it, because the agent's declarations are loaded by the
# workflow and must not load the database with them.
STUDIO_RUN_KIND = "studio-run"


@dataclass(frozen=True)
class StudioRunInput:
    """The run to execute: a run of ``db.studio_runs``, with its project and workspace."""

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID


def studio_workflow_id(run_id: uuid.UUID) -> str:
    """The workflow of a run. It is derived, never stored: one run, one workflow."""
    return f"studio-{run_id}"


def studio_workflow(run: StudioRunInput) -> WorkflowStart:
    """The workflow that executes ``run``, as the API's starter is asked to start it."""
    return WorkflowStart(
        workflow=STUDIO_WORKFLOW,
        workflow_id=studio_workflow_id(run.run_id),
        task_queue=STUDIO_TASK_QUEUE,
        input=run,
    )
