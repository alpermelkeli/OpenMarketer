"""Requesting a research run: store the run, then hand it to the workflow engine.

The order is the point, as it is for an analysis. The run is committed before
its workflow is started, so the worker finds the row it is asked to execute. If
the workflow cannot be started, the run is marked failed at once: nobody would
ever execute this one.

A project may have several research runs at once. Unlike an analysis, a run
reads nothing of the project's own and pays for nothing but its model calls, so
a second request does not make the first one pay twice; a person may well want
answers to two questions at the same time.

Which workflow executes the run is the contract's (``research_workflow``); how
a workflow is started is the caller's business, and it passes a function. This
module does not wait for the run and does not read its status.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.research_runs import (
    ResearchRun,
    mark_run_failed,
    request_research_run,
)
from openmarketer_core.db.session import transaction
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.research.workflow_contract import ResearchRunInput, research_workflow
from openmarketer_core.workflow_start import WorkflowNotStarted, WorkflowStart

# Who asked for a run: a person through the API, the orchestrator's chat, or another run.
# Only a person can start one today; the values exist because the run's record keeps one.
REQUESTED_BY_A_PERSON = "person"
REQUESTED_BY = (REQUESTED_BY_A_PERSON, "orchestrator", "run")
WORKFLOW_NOT_STARTED = "the research could not be handed to a worker"


class ResearchNotStarted(Exception):
    """The run was stored, but no workflow could be started for it; it is now failed."""

    def __init__(self, run_id: uuid.UUID) -> None:
        self.run_id = run_id
        super().__init__(
            f"research run {run_id} could not be handed to a worker and is recorded as failed; "
            "start a new one when the workflow server is reachable"
        )


def request_research(
    sessions: sessionmaker[Session],
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    request: ResearchRequest,
    start_workflow: Callable[[WorkflowStart], None],
    requested_by: str = REQUESTED_BY_A_PERSON,
) -> ResearchRun:
    """Queue a research run for the project and start its workflow.

    Raises ``ProjectNotFound`` when the workspace has no such project, and
    ``ResearchNotStarted`` when the workflow could not be started.
    """
    scope = {"workspace_id": workspace_id, "project_id": project_id}
    with transaction(sessions) as session:
        run = request_research_run(session, **scope, request=request, requested_by=requested_by)
    try:
        start_workflow(research_workflow(ResearchRunInput(run_id=run.id, **scope)))
    except WorkflowNotStarted as e:
        with transaction(sessions) as session:
            mark_run_failed(session, run_id=run.id, error=WORKFLOW_NOT_STARTED, **scope)
        raise ResearchNotStarted(run.id) from e
    return run
