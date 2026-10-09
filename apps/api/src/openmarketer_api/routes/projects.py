"""Projects: registering the repository of a product, reading it back and listing them.

A project comes with where its review stands (latest draft, latest approved
version, unfinished run), so the list needs no further request per project.
Changing or deleting a project is not here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from openmarketer_api.dependencies import DbSession
from openmarketer_api.errors import problems
from openmarketer_api.identity import CurrentWorkspaceId
from openmarketer_api.pagination import DEFAULT_PAGE_SIZE, CreatedBefore, PageLimit, created_cursor
from openmarketer_core.db.projects import (
    ProjectOverview,
    StoredProject,
    create_project,
    list_projects,
    project_overview,
)

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
    latest_draft_version: int | None = Field(
        description="The highest draft version. It can be lower than `latest_approved_version`."
    )
    latest_approved_version: int | None = Field(description="The highest approved version.")
    unfinished_run_id: uuid.UUID | None = Field(
        description="The analysis run that is queued or running; a project has at most one."
    )


class ProjectListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projects: list[ProjectResponse]
    next_cursor: str | None = Field(
        description="Null on the last page; otherwise the `cursor` of the next request."
    )


def _response(overview: ProjectOverview) -> ProjectResponse:
    project = overview.project
    return ProjectResponse(
        id=project.id,
        name=project.name,
        repository_url=project.source_repo_url,
        created_at=project.created_at,
        latest_draft_version=overview.latest_draft_version,
        latest_approved_version=overview.latest_approved_version,
        unfinished_run_id=overview.unfinished_run_id,
    )


def _new(project: StoredProject) -> ProjectOverview:
    """A project that was just created: nothing has been analysed or reviewed yet."""
    return ProjectOverview(
        project=project,
        latest_draft_version=None,
        latest_approved_version=None,
        unfinished_run_id=None,
    )


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
    return _response(_new(project))


@router.get("", operation_id="listProjects", response_model=ProjectListResponse)
def list_all(
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    before: CreatedBefore,
    limit: PageLimit = DEFAULT_PAGE_SIZE,
) -> ProjectListResponse:
    """The workspace's projects, newest first, one page at a time."""
    page = list_projects(session, workspace_id=workspace_id, limit=limit, before=before)
    return ProjectListResponse(
        projects=[_response(overview) for overview in page.items],
        next_cursor=created_cursor(page.next_before),
    )


@router.get(
    "/{project_id}",
    operation_id="getProject",
    response_model=ProjectResponse,
    responses=problems(404),
)
def get(
    project_id: uuid.UUID, session: DbSession, workspace_id: CurrentWorkspaceId
) -> ProjectResponse:
    """One project of the workspace, with where its review stands now."""
    overview = project_overview(session, workspace_id=workspace_id, project_id=project_id)
    return _response(overview)
