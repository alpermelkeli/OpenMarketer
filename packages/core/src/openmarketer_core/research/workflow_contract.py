"""What the API and the worker agree on to run a research run as a durable workflow.

The API starts a workflow for a run it has stored (``db.research_runs``); the
worker executes it. Entry points do not import each other, so the names both
need are here: plain values, with no import of the workflow engine.
``research_workflow`` puts them together as the ``WorkflowStart`` the API's
starter is given. Where the engine is, which both also need, is not about
research: ``workflow_server.py``.

The input names the run and nothing else. The engine stores every input in its
history, where it can be read, so no question, source or report ever belongs in
it; the worker reads the run and its request from the database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from openmarketer_core.workflow_start import WorkflowStart

RESEARCH_WORKFLOW = "Research"
RESEARCH_TASK_QUEUE = "openmarketer-research"
# The kind of run a research run is: what the researcher's agent is declared with, and what
# the name of the thread of a run's checkpoints starts with (``db.research_runs.run_thread_id``).
# It is here, with no import beside it, because the agent's declarations are loaded by the
# workflow and must not load the database with them.
RESEARCH_RUN_KIND = "research-run"


@dataclass(frozen=True)
class ResearchRunInput:
    """The run to execute: a run of ``db.research_runs``, with its project and workspace."""

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID


def research_workflow_id(run_id: uuid.UUID) -> str:
    """The workflow of a run. It is derived, never stored: one run, one workflow."""
    return f"research-{run_id}"


def research_workflow(run: ResearchRunInput) -> WorkflowStart:
    """The workflow that executes ``run``, as the API's starter is asked to start it."""
    return WorkflowStart(
        workflow=RESEARCH_WORKFLOW,
        workflow_id=research_workflow_id(run.run_id),
        task_queue=RESEARCH_TASK_QUEUE,
        input=run,
    )
