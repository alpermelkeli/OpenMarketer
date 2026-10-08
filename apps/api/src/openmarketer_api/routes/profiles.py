"""Product Profile review: read the latest draft or approved version, edit, approve.

The approver is the current user. No request field names an approver, and no
route approves on behalf of a model. Listing versions or reading one by number
is not here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Path
from pydantic import BaseModel, ConfigDict

from openmarketer_api.dependencies import DbSession
from openmarketer_api.errors import ProfileNotFound, problems
from openmarketer_api.identity import CurrentUserId, CurrentWorkspaceId
from openmarketer_core.db.models import ProfileStatus
from openmarketer_core.db.profile_versions import (
    ProfileVersion,
    approve_profile_version,
    latest_approved_profile,
    latest_draft_profile,
    save_edited_profile,
)
from openmarketer_core.profile import ProductProfile

router = APIRouter(prefix="/projects/{project_id}/profile", tags=["profiles"])

VersionNumber = Annotated[int, Path(ge=1)]


class SaveProfileEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: ProductProfile


class ProfileVersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: uuid.UUID
    version: int
    status: ProfileStatus
    profile: ProductProfile
    created_at: datetime
    approved_by: uuid.UUID | None
    approved_at: datetime | None


def _response(stored: ProfileVersion) -> ProfileVersionResponse:
    return ProfileVersionResponse(
        project_id=stored.project_id,
        version=stored.version,
        status=stored.status,
        profile=stored.profile,
        created_at=stored.created_at,
        approved_by=stored.approved_by,
        approved_at=stored.approved_at,
    )


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
)
def approve(
    project_id: uuid.UUID,
    version: VersionNumber,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    approver: CurrentUserId,
) -> ProfileVersionResponse:
    """Approve one version as the current user. The request has no body."""
    approved = approve_profile_version(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        version=version,
        approved_by=approver,
    )
    return _response(approved)
