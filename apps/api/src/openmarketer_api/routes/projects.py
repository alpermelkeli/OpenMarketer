"""Projects: registering the repository of a product. Listing and changing projects is not here."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, StringConstraints

from openmarketer_api.dependencies import DbSession
from openmarketer_api.errors import problems
from openmarketer_api.identity import CurrentWorkspaceId
from openmarketer_core.db.projects import create_project

router = APIRouter(prefix="/projects", tags=["projects"])


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    repository_url: Annotated[str, StringConstraints(max_length=2000)]


class ProjectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    name: str
    repository_url: str
    created_at: datetime


@router.post(
    "",
    operation_id="createProject",
    status_code=201,
    response_model=ProjectResponse,
    responses=problems(400, 409),
)
def create(
    body: CreateProjectRequest, session: DbSession, workspace_id: CurrentWorkspaceId
) -> ProjectResponse:
    """Register a repository. Only https:// URLs are accepted; nothing is cloned yet."""
    project = create_project(
        session, workspace_id=workspace_id, name=body.name, source_repo_url=body.repository_url
    )
    return ProjectResponse(
        id=project.id,
        name=project.name,
        repository_url=project.source_repo_url,
        created_at=project.created_at,
    )
