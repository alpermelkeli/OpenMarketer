"""Research: request a research run, poll it, list the runs, read a report and delete a run.

The research runs in the worker. A run's state and its report are read from the
database only; no route asks the workflow engine about it. A project may have
several research runs at once. Cancelling a run is not here; a finished run can
be deleted, with its report and the pages it fetched.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from openmarketer_api.dependencies import DbSession, Pages, Sessions, StartWorkflow, no_request_body
from openmarketer_api.errors import problems
from openmarketer_api.identity import CurrentWorkspaceId
from openmarketer_api.pagination import DEFAULT_PAGE_SIZE, CreatedBefore, PageLimit, created_cursor
from openmarketer_core.db.models import ResearchRunStatus
from openmarketer_core.db.research_runs import (
    ResearchRun,
    delete_research_run,
    list_research_runs,
    research_run,
)
from openmarketer_core.db.research_store import ResearchReport, research_report
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.research.request import request_research

router = APIRouter(prefix="/projects/{project_id}/researches", tags=["research"])


class ResearchRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    project_id: uuid.UUID
    status: ResearchRunStatus
    requested_by: str = Field(
        description="Who asked for the run: `person`, `orchestrator` (the chat) or `run`."
    )
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    report_id: uuid.UUID | None


class ResearchRunListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runs: list[ResearchRunResponse]
    next_cursor: str | None = Field(
        description="Null on the last page; otherwise the `cursor` of the next request."
    )


class ResearchReportResponse(BaseModel):
    """A finished run's report: its findings, each with the pages it rests on."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    requested_by: str
    questions: list[str]
    findings: list[dict[str, Any]]
    notes: list[str] = Field(description="What the code dropped or worked around on the way.")
    cost_usd: float
    model: str
    created_at: datetime


def _response(run: ResearchRun) -> ResearchRunResponse:
    return ResearchRunResponse(
        id=run.id,
        project_id=run.project_id,
        status=run.status,
        requested_by=run.requested_by,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error=run.error,
        report_id=run.report_id,
    )


def _report_response(report: ResearchReport) -> ResearchReportResponse:
    return ResearchReportResponse(
        id=report.id,
        project_id=report.project_id,
        run_id=report.run_id,
        requested_by=report.requested_by,
        questions=report.questions,
        findings=report.findings,
        notes=report.notes,
        cost_usd=report.cost_usd,
        model=report.model,
        created_at=report.created_at,
    )


@router.post(
    "",
    operation_id="startResearch",
    status_code=202,
    response_model=ResearchRunResponse,
    responses=problems(404, 503),
)
def start(
    project_id: uuid.UUID,
    workspace_id: CurrentWorkspaceId,
    sessions: Sessions,
    start_workflow: StartWorkflow,
    request: ResearchRequest,
) -> ResearchRunResponse:
    """Queue a research run for the questions and return the run to poll.

    A worker executes it later; a project may have several runs at once. 503
    `research_not_started` when the run could not be handed to a worker: it is
    then stored as failed and a new one can be started.
    """
    run = request_research(
        sessions,
        workspace_id=workspace_id,
        project_id=project_id,
        request=request,
        start_workflow=start_workflow,
    )
    return _response(run)


@router.get(
    "",
    operation_id="listResearches",
    response_model=ResearchRunListResponse,
    responses=problems(404),
)
def list_all(
    project_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    before: CreatedBefore,
    limit: PageLimit = DEFAULT_PAGE_SIZE,
) -> ResearchRunListResponse:
    """The project's research runs as they are now, newest first, one page at a time."""
    page = list_research_runs(
        session, workspace_id=workspace_id, project_id=project_id, limit=limit, before=before
    )
    return ResearchRunListResponse(
        runs=[_response(run) for run in page.items],
        next_cursor=created_cursor(page.next_before),
    )


@router.get(
    "/{run_id}",
    operation_id="getResearch",
    response_model=ResearchRunResponse,
    responses=problems(404),
)
def get(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
) -> ResearchRunResponse:
    """The run as it is now: queued, running, succeeded (with the report) or failed."""
    run = research_run(session, workspace_id=workspace_id, project_id=project_id, run_id=run_id)
    return _response(run)


@router.get(
    "/{run_id}/report",
    operation_id="getResearchReport",
    response_model=ResearchReportResponse,
    responses=problems(404),
)
def report(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
) -> ResearchReportResponse:
    """The report of a run that succeeded, with the evidence of every finding.

    404 `research_report_not_found` while the run has no report: it is
    unfinished or it failed.
    """
    stored = research_report(
        session, workspace_id=workspace_id, project_id=project_id, run_id=run_id
    )
    return _report_response(stored)


@router.delete(
    "/{run_id}",
    operation_id="deleteResearch",
    status_code=204,
    responses=problems(404, 409, 503),
    dependencies=[Depends(no_request_body)],
)
def delete(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    pages: Pages,
) -> None:
    """Delete a finished run with its report and the pages it fetched, for good.

    409 `research_unfinished` while the run is queued or running: the worker
    would store pages or a report for a run that is gone. 503
    `storage_unavailable` when the pages' objects could not be removed from
    object storage; the run stays then, and can be deleted again. The request
    takes no body; one is a 422.
    """
    keys = delete_research_run(
        session, workspace_id=workspace_id, project_id=project_id, run_id=run_id
    )
    for key in keys:
        pages.remove(key)
