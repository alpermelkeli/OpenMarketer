"""Requesting a writing run: store the run, then hand it to the workflow engine.

The order is the point, as it is for research and the studio. The run is committed before its
workflow is started, so the worker finds the row it is asked to execute. If the workflow
cannot be started, the run is marked failed at once: nobody would ever execute this one.

A project may have several writing runs at once; each writes one post and proposes it.

Which workflow executes the run is the contract's (``writing_workflow``); how a workflow is
started is the caller's business, and it passes a function. This module does not wait for the
run and does not read its status.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.session import transaction
from openmarketer_core.db.writing_runs import WritingRun, mark_run_failed, request_writing_run
from openmarketer_core.workflow_start import WorkflowNotStarted, WorkflowStart
from openmarketer_core.writing.brief import WritingBrief
from openmarketer_core.writing.workflow_contract import WritingRunInput, writing_workflow

# Who asked for a run: a person through the API, the orchestrator's chat, or the action of
# another run. The run's record keeps one of them (the column's CHECK lists the same three).
REQUESTED_BY_A_PERSON = "person"
REQUESTED_BY_THE_CHAT = "orchestrator"
REQUESTED_BY_A_RUN = "run"
REQUESTED_BY = (REQUESTED_BY_A_PERSON, REQUESTED_BY_THE_CHAT, REQUESTED_BY_A_RUN)
WORKFLOW_NOT_STARTED = "the post could not be handed to a worker"


class WritingNotStarted(Exception):
    """The run was stored, but no workflow could be started for it; it is now failed."""

    def __init__(self, run_id: uuid.UUID) -> None:
        self.run_id = run_id
        super().__init__(
            f"writing run {run_id} could not be handed to a worker and is recorded as failed; "
            "start a new one when the workflow server is reachable"
        )


def request_post(
    sessions: sessionmaker[Session],
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    brief: WritingBrief,
    start_workflow: Callable[[WorkflowStart], None],
    requested_by: str = REQUESTED_BY_A_PERSON,
    chat_thread_id: uuid.UUID | None = None,
) -> WritingRun:
    """Queue a writing run for the project and start its workflow.

    ``chat_thread_id`` is the chat whose turn asks for the run, which hears back when it
    ends. Raises ``ProjectNotFound`` when the workspace has no such project, ``ChatNotFound``
    when the project has no such chat, ``MediaAssetNotFound`` or ``AssetNotUsable`` when the
    brief's asset is not the project's or cannot go with a post (rejected, still being made,
    without an image, or a draft of a failed run), ``ResearchRunNotFound`` or
    ``ResearchRunNotUsable`` when its research run is not the project's or has not succeeded,
    and ``WritingNotStarted`` when the workflow could not be started. Nothing is stored when
    a check fails.
    """
    with transaction(sessions) as session:
        run = request_writing_run(
            session,
            workspace_id=workspace_id,
            project_id=project_id,
            brief=brief,
            requested_by=requested_by,
            chat_thread_id=chat_thread_id,
        )
    try:
        start_workflow(
            writing_workflow(
                WritingRunInput(workspace_id=workspace_id, project_id=project_id, run_id=run.id)
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
        raise WritingNotStarted(run.id) from e
    return run
