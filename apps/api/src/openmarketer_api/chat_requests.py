"""The chat turn's request actions, answered from the API's database and workflow engine.

A chat turn runs in the API's process, so what its request actions ask for (core's
``ChatRequests``) is answered here: a research run or a studio run of the turn's project is
stored and its workflow started, the way the research and studio routes do, and answers with
the run as the chat reads runs (``ResearchRunReading``, ``StudioRunReading``). A run whose
workflow could not be started is recorded as failed by the use case, and the port answers
``ResearchNotStarted`` or ``StudioNotStarted`` with a sentence, which the tool turns into
words for the operator; so does a studio request that names a reference asset the project
does not have, for which nothing is stored.

This module holds no rule of its own: ``request_research`` and ``request_asset`` are core's
use cases, the starter is
the API's workflow adapter (the same function a route gets), and who asked is fixed to the
orchestrator's chat. An adapter is built for one turn and holds the turn's workspace,
project and chat, so a tool cannot start a run elsewhere, and the run is the chat's: it is
stored with the chat's id, and the chat hears back from it when it ends.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.research_runs import ResearchRun
from openmarketer_core.db.studio_runs import ReferenceAssetNotFound
from openmarketer_core.orchestrator import requests as chat
from openmarketer_core.orchestrator.reads import ResearchRunReading, StudioRunReading
from openmarketer_core.orchestrator.reads_postgres import studio_run_reading
from openmarketer_core.research import request as research
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.studio import request as studio
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.workflow_start import WorkflowStart

# What the operator is told when a studio request names an image the project's library lacks.
REFERENCE_NOT_FOUND = "a reference image is not in this project's Studio library"


@dataclass(frozen=True)
class PostgresChatRequests:
    """Core's ``ChatRequests`` for one chat turn: this API's database and starter."""

    sessions: sessionmaker[Session]
    start_workflow: Callable[[WorkflowStart], None]
    workspace_id: uuid.UUID
    project_id: uuid.UUID
    chat_id: uuid.UUID

    def start_research(self, request: ResearchRequest) -> ResearchRunReading:
        """Store a research run for the turn's project and start its workflow.

        Raises the port's ``ResearchNotStarted``, in the use case's words, when the run could
        not be handed to a worker; the run is recorded as failed by then.
        """
        try:
            run = research.request_research(
                self.sessions,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                request=request,
                start_workflow=self.start_workflow,
                requested_by=research.REQUESTED_BY_THE_CHAT,
                chat_thread_id=self.chat_id,
            )
        except research.ResearchNotStarted as e:
            raise chat.ResearchNotStarted(str(e)) from e
        return _reading(run)

    def start_creative(self, request: AssetRequest) -> StudioRunReading:
        """Store a studio run for the turn's project and start its workflow.

        Raises the port's ``StudioNotStarted`` when a reference asset is not the project's
        (nothing is stored) and when the run could not be handed to a worker (the run is
        recorded as failed by then).
        """
        try:
            run = studio.request_asset(
                self.sessions,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                request=request,
                start_workflow=self.start_workflow,
                requested_by=studio.REQUESTED_BY_THE_CHAT,
                chat_thread_id=self.chat_id,
            )
        except ReferenceAssetNotFound as e:
            raise chat.StudioNotStarted(REFERENCE_NOT_FOUND) from e
        except studio.StudioNotStarted as e:
            raise chat.StudioNotStarted(str(e)) from e
        return studio_run_reading(run, request)


def _reading(run: ResearchRun) -> ResearchRunReading:
    return ResearchRunReading(
        id=run.id,
        status=run.status,
        requested_by=run.requested_by,
        error=run.error,
        report_id=run.report_id,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )
