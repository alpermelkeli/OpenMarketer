"""Product Profile review: read versions, list them, edit, approve.

The approver is the current user. No request field names an approver, and no
route approves on behalf of a model. A version is named by its number; the list
of versions carries no profile, so it stays small however large the profiles
are. Comparing two versions is not here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field

from openmarketer_api.dependencies import DbSession, no_request_body
from openmarketer_api.errors import ProfileNotFound, problems
from openmarketer_api.identity import CurrentUserId, CurrentWorkspaceId
from openmarketer_api.pagination import (
    DEFAULT_PAGE_SIZE,
    HIGHEST_VERSION,
    PageLimit,
    VersionBefore,
    version_cursor,
)
from openmarketer_core.db.models import ProfileStatus
from openmarketer_core.db.profile_versions import (
    ProfileVersion,
    ProfileVersionSummary,
    approve_profile_version,
    latest_approved_profile,
    latest_draft_profile,
    list_profile_versions,
    profile_version,
    save_edited_profile,
)
from openmarketer_core.profile import ProductProfile

router = APIRouter(prefix="/projects/{project_id}/profile", tags=["profiles"])

# Version numbers are a 32-bit integer column; a larger number is no version, not a database error.
VersionNumber = Annotated[int, Path(ge=1, le=HIGHEST_VERSION)]


class SaveProfileEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: ProductProfile


class ProfileVersionSummaryResponse(BaseModel):
    """A version without its profile."""

    model_config = ConfigDict(extra="forbid")

    project_id: uuid.UUID
    version: int
    status: ProfileStatus
    edited_from_version: int | None = Field(
        description="The version this one is an edit of; null when an analysis stored it."
    )
    commit_sha: str | None = Field(
        description=(
            "The commit of the repository that was analysed. An edit keeps the commit of"
            " the version it was made from."
        )
    )
    created_at: datetime
    approved_by: uuid.UUID | None
    approved_at: datetime | None


class ProfileVersionResponse(ProfileVersionSummaryResponse):
    """A version with its profile."""

    profile: ProductProfile


class ProfileVersionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    versions: list[ProfileVersionSummaryResponse]
    next_cursor: str | None = Field(
        description="Null on the last page; otherwise the `cursor` of the next request."
    )


def _summary(stored: ProfileVersion | ProfileVersionSummary) -> ProfileVersionSummaryResponse:
    return ProfileVersionSummaryResponse(
        project_id=stored.project_id,
        version=stored.version,
        status=stored.status,
        edited_from_version=stored.edited_from_version,
        commit_sha=stored.commit_sha,
        created_at=stored.created_at,
        approved_by=stored.approved_by,
        approved_at=stored.approved_at,
    )


def _response(stored: ProfileVersion) -> ProfileVersionResponse:
    return ProfileVersionResponse(**_summary(stored).model_dump(), profile=stored.profile)


@router.get(
    "/draft",
    operation_id="getDraftProfile",
    response_model=ProfileVersionResponse,
    responses=problems(404),
)
def get_draft(
    project_id: uuid.UUID, session: DbSession, workspace_id: CurrentWorkspaceId
) -> ProfileVersionResponse:
    """The draft with the highest version number. It can be older than the approved profile."""
    draft = latest_draft_profile(session, workspace_id=workspace_id, project_id=project_id)
    if draft is None:
        raise ProfileNotFound(f"project {project_id} has no draft profile")
    return _response(draft)


@router.get(
    "/approved",
    operation_id="getApprovedProfile",
    response_model=ProfileVersionResponse,
    responses=problems(404),
)
def get_approved(
    project_id: uuid.UUID, session: DbSession, workspace_id: CurrentWorkspaceId
) -> ProfileVersionResponse:
    """The approved version with the highest version number."""
    approved = latest_approved_profile(session, workspace_id=workspace_id, project_id=project_id)
    if approved is None:
        raise ProfileNotFound(f"project {project_id} has no approved profile")
    return _response(approved)


@router.get(
    "/versions",
    operation_id="listProfileVersions",
    response_model=ProfileVersionListResponse,
    responses=problems(404),
)
def list_versions(
    project_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    before: VersionBefore,
    limit: PageLimit = DEFAULT_PAGE_SIZE,
) -> ProfileVersionListResponse:
    """The project's versions without their profiles, highest number first, a page at a time."""
    page = list_profile_versions(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        limit=limit,
        before_version=before,
    )
    return ProfileVersionListResponse(
        versions=[_summary(version) for version in page.items],
        next_cursor=version_cursor(page.next_before),
    )


@router.get(
    "/versions/{version}",
    operation_id="getProfileVersion",
    response_model=ProfileVersionResponse,
    responses=problems(404),
)
def get_version(
    project_id: uuid.UUID,
    version: VersionNumber,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
) -> ProfileVersionResponse:
    """One version with its profile, draft or approved."""
    stored = profile_version(
        session, workspace_id=workspace_id, project_id=project_id, version=version
    )
    return _response(stored)


@router.post(
    "/versions/{version}/edits",
    operation_id="saveProfileEdit",
    status_code=201,
    response_model=ProfileVersionResponse,
    responses=problems(404),
)
def save_edit(
    project_id: uuid.UUID,
    version: VersionNumber,
    body: SaveProfileEditRequest,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
) -> ProfileVersionResponse:
    """Store an edit of ``version`` as a new draft version. The edited version is not changed."""
    edited = save_edited_profile(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        edited_version=version,
        profile=body.profile,
    )
    return _response(edited)


@router.post(
    "/versions/{version}/approval",
    operation_id="approveProfileVersion",
    response_model=ProfileVersionResponse,
    responses=problems(404, 409),
    dependencies=[Depends(no_request_body)],
)
def approve(
    project_id: uuid.UUID,
    version: VersionNumber,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    approver: CurrentUserId,
) -> ProfileVersionResponse:
    """Approve one version as the current user. The request takes no body; one is a 422."""
    approved = approve_profile_version(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        version=version,
        approved_by=approver,
    )
    return _response(approved)
