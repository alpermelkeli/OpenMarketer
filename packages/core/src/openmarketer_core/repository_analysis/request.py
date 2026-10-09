"""Requesting an analysis: store the run, then hand it to the workflow engine.

The order is the point. The run is committed before its workflow is started, so
the worker finds the row it is asked to execute. If the workflow cannot be
started, the run is marked failed at once: an unfinished run blocks its
project, and nobody would ever execute this one.

How a workflow is started is the caller's business; it passes a function. This
module does not wait for the analysis and does not read a run's status.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.analysis_runs import AnalysisRun, mark_run_failed, request_analysis_run
from openmarketer_core.db.projects import get_project
from openmarketer_core.db.session import transaction
from openmarketer_core.repository_analysis.intake import remote_repository_url
from openmarketer_core.repository_analysis.workflow_contract import AnalyzeRepositoryInput

WORKFLOW_NOT_STARTED = "the analysis could not be handed to a worker"


class WorkflowNotStarted(Exception):
    """The workflow engine could not be reached, or refused to start the workflow.

    Raised by the function that starts workflows. A workflow that exists
    already is not this error: for the caller it has been started.
    """


class AnalysisNotStarted(Exception):
    """The run was stored, but no workflow could be started for it; it is now failed."""

    def __init__(self, run_id: uuid.UUID) -> None:
        self.run_id = run_id
        super().__init__(
            f"analysis run {run_id} could not be handed to a worker and is recorded as failed; "
            "start a new one when the workflow server is reachable"
        )


def request_analysis(
    sessions: sessionmaker[Session],
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    start_workflow: Callable[[AnalyzeRepositoryInput], None],
) -> AnalysisRun:
    """Queue an analysis of the project's repository and start its workflow.

    Raises ``ProjectNotFound``; ``IntakeError`` when the project's repository is
    not an ``https://`` URL (the command line stores local folders, which only
    it may read); ``AnalysisAlreadyRunning``; and ``AnalysisNotStarted``.
    """
    scope = {"workspace_id": workspace_id, "project_id": project_id}
    with transaction(sessions) as session:
        project = get_project(session, **scope)
        remote_repository_url(project.source_repo_url)
        run = request_analysis_run(session, **scope)
    try:
        start_workflow(AnalyzeRepositoryInput(run_id=run.id, **scope))
    except WorkflowNotStarted as e:
        with transaction(sessions) as session:
            mark_run_failed(session, run_id=run.id, error=WORKFLOW_NOT_STARTED, **scope)
        raise AnalysisNotStarted(run.id) from e
    return run
