"""Analyses: request the analysis of a project's repository, poll the run, list the runs.

The analysis runs in the worker. A run's state is read from the database only;
no route asks the workflow engine about it. Cancelling a run is not here.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from openmarketer_api.dependencies import DbSession, Sessions, StartWorkflow, no_request_body
from openmarketer_api.errors import problems
from openmarketer_api.identity import CurrentWorkspaceId
from openmarketer_api.pagination import DEFAULT_PAGE_SIZE, CreatedBefore, PageLimit, created_cursor
from openmarketer_core.analysis_request import request_analysis
from openmarketer_core.db.analysis_runs import AnalysisRun, analysis_run, list_analysis_runs
from openmarketer_core.db.models import AnalysisRunStatus

router = APIRouter(prefix="/projects/{project_id}/analyses", tags=["analyses"])


class AnalysisRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    project_id: uuid.UUID
    status: AnalysisRunStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    profile_version: int | None


class AnalysisRunListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runs: list[AnalysisRunResponse]
    next_cursor: str | None = Field(
        description="Null on the last page; otherwise the `cursor` of the next request."
    )


def _response(run: AnalysisRun) -> AnalysisRunResponse:
    return AnalysisRunResponse(
        id=run.id,
        project_id=run.project_id,
        status=run.status,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error=run.error,
        profile_version=run.profile_version,
    )


@router.post(
    "",
    operation_id="startAnalysis",
    status_code=202,
    response_model=AnalysisRunResponse,
    responses=problems(400, 404, 409),
    dependencies=[Depends(no_request_body)],
)
def start(
    project_id: uuid.UUID,
    workspace_id: CurrentWorkspaceId,
    sessions: Sessions,
    start_workflow: StartWorkflow,
) -> AnalysisRunResponse:
    """Queue an analysis and return the run to poll. A worker executes it later.

    409 while the project has an unfinished run. 503 `analysis_not_started` when
    the run could not be handed to a worker: it is then stored as failed, does
    not block the project, and a new one can be started.
    """
    run = request_analysis(
        sessions, workspace_id=workspace_id, project_id=project_id, start_workflow=start_workflow
    )
    return _response(run)


@router.get(
    "",
    operation_id="listAnalyses",
    response_model=AnalysisRunListResponse,
    responses=problems(404),
)
def list_all(
    project_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    before: CreatedBefore,
    limit: PageLimit = DEFAULT_PAGE_SIZE,
) -> AnalysisRunListResponse:
    """The project's runs as they are now, newest first, one page at a time.

    The first one is the unfinished run, when the project has one.
    """
    page = list_analysis_runs(
        session, workspace_id=workspace_id, project_id=project_id, limit=limit, before=before
    )
    return AnalysisRunListResponse(
        runs=[_response(run) for run in page.items],
        next_cursor=created_cursor(page.next_before),
    )


@router.get(
    "/{run_id}",
    operation_id="getAnalysis",
    response_model=AnalysisRunResponse,
    responses=problems(404),
)
def get(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
) -> AnalysisRunResponse:
    """The run as it is now: queued, running, succeeded (with the profile version) or failed."""
    run = analysis_run(session, workspace_id=workspace_id, project_id=project_id, run_id=run_id)
    return _response(run)
