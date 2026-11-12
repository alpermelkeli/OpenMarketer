"""The chat turn's request actions, answered from the API's database and workflow engine.

A chat turn runs in the API's process, so what its request actions ask for (core's
``ChatRequests``) is answered here: a research run, a studio run, a writing run or an
orchestrator task of the turn's project is stored and its workflow started, the way the
research, studio and writing routes do, and answers with the run as the chat reads runs
(``ResearchRunReading``, ``StudioRunReading``, ``WritingRunReading``, ``TaskReading``). A run
whose workflow could not be started is recorded as failed by the use case, and the port answers
``ResearchNotStarted``, ``StudioNotStarted``, ``WritingNotStarted`` or ``TaskNotStarted`` with a
sentence, which the tool turns into words for the operator; so does a studio request that names
a reference asset the project does not have, and a writing brief whose asset or research run is
not the project's or cannot go with a post, for which nothing is stored.

This module holds no rule of its own: ``request_research``, ``request_asset``,
``request_post`` and ``request_task`` are core's use cases, the starter is
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

from openmarketer_core.db.media_assets import AssetNotUsable, MediaAssetNotFound
from openmarketer_core.db.research_runs import ResearchRun, ResearchRunNotFound
from openmarketer_core.db.research_store import ResearchRunNotUsable
from openmarketer_core.db.studio_runs import ReferenceAssetNotFound
from openmarketer_core.orchestrator import requests as chat
from openmarketer_core.orchestrator.reads import (
    ResearchRunReading,
    StudioRunReading,
    TaskReading,
    WritingRunReading,
)
from openmarketer_core.orchestrator.reads_postgres import (
    studio_run_reading,
    task_reading,
    writing_run_reading,
)
from openmarketer_core.orchestrator.task import request as task
from openmarketer_core.orchestrator.task.plan import TaskRequest
from openmarketer_core.research import request as research
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.studio import request as studio
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.workflow_start import WorkflowStart
from openmarketer_core.writing import request as writing
from openmarketer_core.writing.brief import WritingBrief

# What the operator is told when a studio request names an image the project's library lacks.
REFERENCE_NOT_FOUND = "a reference image is not in this project's Studio library"
# What the operator is told when a writing brief names an input the project cannot give it.
ASSET_NOT_FOUND = "the post's asset is not in this project's Studio library"
ASSET_NOT_USABLE = "the post's asset cannot go with a post: {reason}"
RESEARCH_NOT_FOUND = "the post's research run is not one of this project's"
RESEARCH_NOT_USABLE = "the post's research run has not succeeded, so it has no findings to use"


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

    def start_writing(self, brief: WritingBrief) -> WritingRunReading:
        """Store a writing run for the turn's project and chat and start its workflow.

        Raises the port's ``WritingNotStarted`` when the brief's asset or research run is not
        the project's or cannot go with a post (nothing is stored), and when the run could not
        be handed to a worker (the run is recorded as failed by then).
        """
        try:
            run = writing.request_post(
                self.sessions,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                brief=brief,
                start_workflow=self.start_workflow,
                requested_by=writing.REQUESTED_BY_THE_CHAT,
                chat_thread_id=self.chat_id,
            )
        except MediaAssetNotFound as e:
            raise chat.WritingNotStarted(ASSET_NOT_FOUND) from e
        except AssetNotUsable as e:
            # The reason is code's own sentence (``db/media_assets.py``), and names no value.
            raise chat.WritingNotStarted(ASSET_NOT_USABLE.format(reason=e.reason)) from e
        except ResearchRunNotFound as e:
            raise chat.WritingNotStarted(RESEARCH_NOT_FOUND) from e
        except ResearchRunNotUsable as e:
            raise chat.WritingNotStarted(RESEARCH_NOT_USABLE) from e
        except writing.WritingNotStarted as e:
            raise chat.WritingNotStarted(writing.WORKFLOW_NOT_STARTED) from e
        return writing_run_reading(run, brief.model_dump(mode="json"))

    def start_task(self, request: TaskRequest) -> TaskReading:
        """Store an orchestrator task for the turn's project and chat and start its workflow.

        Raises the port's ``TaskNotStarted`` with a fixed sentence, which names no task, when
        the task could not be handed to a worker; the task is recorded as failed by then.
        """
        try:
            stored = task.request_task(
                self.sessions,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                request=request,
                start_workflow=self.start_workflow,
                requested_by=task.REQUESTED_BY_THE_CHAT,
                chat_thread_id=self.chat_id,
            )
        except task.TaskWorkflowNotStarted as e:
            raise chat.TaskNotStarted(task.WORKFLOW_NOT_STARTED) from e
        return task_reading(stored)


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
