"""Requesting a studio run: store the run, then hand it to the workflow engine.

The order is the point, as it is for research. The run is committed before its workflow is
started, so the worker finds the row it is asked to execute. If the workflow cannot be
started, the run is marked failed at once: nobody would ever execute this one.

A project may have several studio runs at once; each pays for its own generations, under the
budget the worker reserves before each one.

Which workflow executes the run is the contract's (``studio_workflow``); how a workflow is
started is the caller's business, and it passes a function. This module does not wait for the
run and does not read its status.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.session import transaction
from openmarketer_core.db.studio_runs import StudioRun, mark_run_failed, request_studio_run
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.studio.workflow_contract import StudioRunInput, studio_workflow
from openmarketer_core.workflow_start import WorkflowNotStarted, WorkflowStart

# Who asked for a run: a person through the API, the orchestrator's chat, or the action of
# another run. The run's record keeps one of them (the column's CHECK lists the same three).
REQUESTED_BY_A_PERSON = "person"
REQUESTED_BY_THE_CHAT = "orchestrator"
REQUESTED_BY_A_RUN = "run"
REQUESTED_BY = (REQUESTED_BY_A_PERSON, REQUESTED_BY_THE_CHAT, REQUESTED_BY_A_RUN)
WORKFLOW_NOT_STARTED = "the asset could not be handed to a worker"


class StudioNotStarted(Exception):
    """The run was stored, but no workflow could be started for it; it is now failed."""

    def __init__(self, run_id: uuid.UUID) -> None:
        self.run_id = run_id
        super().__init__(
            f"studio run {run_id} could not be handed to a worker and is recorded as failed; "
            "start a new one when the workflow server is reachable"
        )


def request_asset(
    sessions: sessionmaker[Session],
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    request: AssetRequest,
    start_workflow: Callable[[WorkflowStart], None],
    requested_by: str = REQUESTED_BY_A_PERSON,
    chat_thread_id: uuid.UUID | None = None,
) -> StudioRun:
    """Queue a studio run for the project and start its workflow.

    ``chat_thread_id`` is the chat whose turn asks for the run, which hears back when it
    ends. Raises ``ProjectNotFound`` when the workspace has no such project, ``ChatNotFound``
    when the project has no such chat, ``ReferenceAssetNotFound`` when a reference asset is
    not the project's, and ``StudioNotStarted`` when the workflow could not be started.
    """
    with transaction(sessions) as session:
        run = request_studio_run(
            session,
            workspace_id=workspace_id,
            project_id=project_id,
            request=request,
            requested_by=requested_by,
            chat_thread_id=chat_thread_id,
        )
    try:
        start_workflow(
            studio_workflow(
                StudioRunInput(workspace_id=workspace_id, project_id=project_id, run_id=run.id)
            )
        )
    except WorkflowNotStarted as e:
        with transaction(sessions) as session:
            mark_run_failed(
                session,
                workspace_id=workspace_id,
                project_id=project_id,
                run_id=run.id,
                error=WORKFLOW_NOT_STARTED,
            )
        raise StudioNotStarted(run.id) from e
    return run
