"""Analyses: start the analysis of a project's repository, then poll the run."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from openmarketer_api.analysis_runs import AnalysisRun, AnalysisStatus
from openmarketer_api.dependencies import AnalysisRunsDep, DbSession
from openmarketer_api.errors import problems
from openmarketer_api.identity import CurrentWorkspaceId

router = APIRouter(prefix="/projects/{project_id}/analyses", tags=["analyses"])


class AnalysisRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    project_id: uuid.UUID
    status: AnalysisStatus
    created_at: datetime
    finished_at: datetime | None
    error: str | None
    profile_version: int | None


def _response(run: AnalysisRun) -> AnalysisRunResponse:
    return AnalysisRunResponse(
        id=run.id,
        project_id=run.project_id,
        status=run.status,
        created_at=run.created_at,
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
)
def start(
    project_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    runs: AnalysisRunsDep,
) -> AnalysisRunResponse:
    """Start analysing the repository and return the run to poll. The work happens later."""
    return _response(runs.start(session, workspace_id=workspace_id, project_id=project_id))


@router.get(
    "/{run_id}",
    operation_id="getAnalysis",
    response_model=AnalysisRunResponse,
    responses=problems(404),
)
def get(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    workspace_id: CurrentWorkspaceId,
    runs: AnalysisRunsDep,
) -> AnalysisRunResponse:
    """The run as it is now: queued, running, succeeded or failed."""
    return _response(runs.get(workspace_id=workspace_id, project_id=project_id, run_id=run_id))
