"""The creative studio: request an image, poll its run, and decide on the assets runs made.

A studio run is executed by the worker; its state, its assets and their versions are read
from the database only, and no route asks the workflow engine about a run. A project may have
several studio runs at once. A finished run can be deleted, with its assets and every version's
image; cancelling a run is not here.

The library is the project's assets, newest first. An asset is read with its versions: how
each was made (technique, size, provider, model, prompt, parameters, cost) and what the checks
and the verifier found of it. The code a composed version ran in the sandbox is not served:
it is unbounded text a model wrote, kept in the database as provenance, and a version says
only whether it has code and how long it is. A version's image is served from object storage
with the type stored with the version, never one guessed from the bytes or the request, and
only under the asset and the project it belongs to.

A single run is read with what it was asked for (goal, exact copy, platform, style notes and
reference assets), read back strictly; a stored request that no longer fits the schema is
answered as no request rather than as an error. The list of runs leaves the request out.

A person accepts or rejects a draft asset once its run has ended; the decision is recorded as
the current user, never as anything the request names.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field

from openmarketer_api.dependencies import (
    Assets,
    DbSession,
    Sessions,
    StartWorkflow,
    no_request_body,
)
from openmarketer_api.errors import json_problems, problems
from openmarketer_api.identity import CurrentUserId, CurrentWorkspaceId
from openmarketer_api.pagination import DEFAULT_PAGE_SIZE, CreatedBefore, PageLimit, created_cursor
from openmarketer_core.db.media_assets import (
    MediaAsset,
    MediaAssetVersion,
    MediaAssetVersionNotFound,
    accept_asset,
    asset_version,
    asset_versions,
    list_media_assets,
    media_asset,
    reject_asset,
    select_asset_version,
)
from openmarketer_core.db.models import AssetTechnique, MediaAssetStatus, StudioRunStatus
from openmarketer_core.db.pages import MAX_PAGE_SIZE
from openmarketer_core.db.studio_runs import (
    StudioRun,
    delete_studio_run,
    list_studio_runs,
    requested_asset,
    studio_run,
)
from openmarketer_core.studio.asset_store import EXTENSIONS
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.request import request_asset
from openmarketer_core.studio.requests import AssetRequest, StoredAssetRequestInvalid

router = APIRouter(prefix="/projects/{project_id}", tags=["studio"])

# A version's image never changes; it can only be deleted with its run or its project.
IMAGE_CACHE_CONTROL = "private, max-age=86400, immutable"
_IMAGE_CONTENT: dict[str, Any] = {
    mime: {"schema": {"type": "string", "format": "binary"}} for mime in EXTENSIONS
}


class StudioRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    project_id: uuid.UUID
    status: StudioRunStatus
    platform: Platform
    requested_by: str = Field(
        description="Who asked for the run: `person`, `orchestrator` (the chat) or `run`."
    )
    error: str | None
    final_asset_id: uuid.UUID | None = Field(
        description="The asset a run that succeeded or needs review ended with."
    )
    cost_usd: float = Field(description="What the run spent, in USD, once it has ended.")
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class StudioRunRequestResponse(BaseModel):
    """What a run was asked for, as it was stored."""

    model_config = ConfigDict(extra="forbid")

    goal: str
    exact_copy: list[str] = Field(
        description="The lines of text the image must show, exactly; empty for no text."
    )
    platform: Platform
    style_notes: str
    reference_asset_ids: list[uuid.UUID] = Field(
        description="Assets of the project the studio was asked to look at."
    )


class StudioRunDetailResponse(StudioRunResponse):
    request: StudioRunRequestResponse | None = Field(
        description=(
            "What the run was asked for; null when the stored request no longer fits the "
            "request's schema, which a later version may have tightened."
        )
    )


class StudioRunListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runs: list[StudioRunResponse]
    next_cursor: str | None = Field(
        description="Null on the last page; otherwise the `cursor` of the next request."
    )


class MediaAssetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    platform: Platform
    status: MediaAssetStatus
    current_version_id: uuid.UUID | None = Field(
        description="The version the asset stands for now: the run's best candidate."
    )
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    created_at: datetime


class MediaAssetListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assets: list[MediaAssetResponse]
    next_cursor: str | None = Field(
        description="Null on the last page; otherwise the `cursor` of the next request."
    )


class MediaAssetVersionResponse(BaseModel):
    """One candidate of an asset: how it was made and what its reviews found, not its image."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    round: int
    technique: AssetTechnique
    mime: str = Field(description="`image/png`, `image/jpeg` or `image/webp`.")
    width: int
    height: int
    size_bytes: int
    provider: str | None
    model: str | None
    prompt: str | None
    params: dict[str, Any]
    has_code: bool = Field(description="Whether the version was made by code in the sandbox.")
    code_chars: int = Field(description="How long that code is; the code itself is not served.")
    check_report: dict[str, Any] | None = Field(
        description="What the programmatic checks found; null until the version is reviewed."
    )
    verifier_report: dict[str, Any] | None = Field(
        description="What the visual verifier found; null when it was not asked."
    )
    passed: bool | None
    cost_usd: float
    created_at: datetime


class MediaAssetDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset: MediaAssetResponse
    versions: list[MediaAssetVersionResponse] = Field(
        description=f"Newest first; at most {MAX_PAGE_SIZE}, far more than a run makes."
    )


def _run_response(run: StudioRun) -> StudioRunResponse:
    return StudioRunResponse(
        id=run.id,
        project_id=run.project_id,
        status=run.status,
        platform=run.platform,
        requested_by=run.requested_by,
        error=run.error,
        final_asset_id=run.final_asset_id,
        cost_usd=float(run.cost_usd),
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _request_response(request: AssetRequest) -> StudioRunRequestResponse:
    return StudioRunRequestResponse(
        goal=request.goal,
        exact_copy=list(request.exact_copy),
        platform=request.platform,
        style_notes=request.style_notes,
        reference_asset_ids=list(request.reference_asset_ids),
    )


def _asset_response(asset: MediaAsset) -> MediaAssetResponse:
    return MediaAssetResponse(
        id=asset.id,
        project_id=asset.project_id,
        run_id=asset.run_id,
        platform=asset.platform,
        status=asset.status,
        current_version_id=asset.current_version_id,
        decided_by=asset.decided_by,
        decided_at=asset.decided_at,
        created_at=asset.created_at,
    )


def _plain(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return None if value is None else dict(value)


def _version_response(version: MediaAssetVersion) -> MediaAssetVersionResponse:
    return MediaAssetVersionResponse(
        id=version.id,
        round=version.round,
        technique=version.technique,
        mime=version.mime,
        width=version.width,
        height=version.height,
        size_bytes=version.size_bytes,
        provider=version.provider,
        model=version.model,
        prompt=version.prompt,
        params=dict(version.params),
        has_code=version.code is not None,
        code_chars=0 if version.code is None else len(version.code),
        check_report=_plain(version.check_report),
        verifier_report=_plain(version.verifier_report),
        passed=version.passed,
        cost_usd=float(version.cost_usd),
        created_at=version.created_at,
    )


@router.post(
    "/studio-runs",
    operation_id="startStudioRun",
    status_code=202,
    response_model=StudioRunResponse,
    responses=problems(404, 503),
)
def start(
    project_id: uuid.UUID,
    workspace_id: CurrentWorkspaceId,
    sessions: Sessions,
    start_workflow: StartWorkflow,
    request: AssetRequest,
) -> StudioRunResponse:
    """Queue a studio run for the image asked for and return the run to poll.

    A worker executes it later, under the workspace's budget for generated media; a project
    may have several runs at once. A reference asset that is not the project's is a 422
    beside `reference_asset_ids`. 503 `studio_not_started` when the run could not be handed
    to a worker: it is then stored as failed and a new one can be started.
    """
    run = request_asset(
        sessions,
        workspace_id=workspace_id,
        project_id=project_id,
        request=request,
        start_workflow=start_workflow,
    )
    return _run_response(run)


@router.get(
    "/studio-runs",
    operation_id="listStudioRuns",
    response_model=StudioRunListResponse,
    responses=problems(404),
)
def list_runs(
    project_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    before: CreatedBefore,
    limit: PageLimit = DEFAULT_PAGE_SIZE,
) -> StudioRunListResponse:
    """The project's studio runs as they are now, newest first, one page at a time."""
    page = list_studio_runs(
        session, workspace_id=workspace_id, project_id=project_id, limit=limit, before=before
    )
    return StudioRunListResponse(
        runs=[_run_response(run) for run in page.items],
        next_cursor=created_cursor(page.next_before),
    )


@router.get(
    "/studio-runs/{run_id}",
    operation_id="getStudioRun",
    response_model=StudioRunDetailResponse,
    responses=problems(404),
)
def get_run(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
) -> StudioRunDetailResponse:
    """The run as it is now, with what it was asked for.

    Queued, running, succeeded, needs review (with its asset) or failed.
    """
    run = studio_run(session, workspace_id=workspace_id, project_id=project_id, run_id=run_id)
    try:
        request = _request_response(
            requested_asset(
                session, workspace_id=workspace_id, project_id=project_id, run_id=run_id
            )
        )
    except StoredAssetRequestInvalid:
        request = None
    return StudioRunDetailResponse(**_run_response(run).model_dump(), request=request)


@router.delete(
    "/studio-runs/{run_id}",
    operation_id="deleteStudioRun",
    status_code=204,
    responses=problems(404, 409, 503),
    dependencies=[Depends(no_request_body)],
)
def delete_run(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    assets: Assets,
) -> None:
    """Delete a finished run with its assets and every version's image, for good.

    An accepted asset goes too. 409 `studio_run_unfinished` while the run is queued or
    running: the worker would store images for a run that is gone. 503 `storage_unavailable`
    when the images could not be removed from object storage; the run stays then, and can be
    deleted again. The request takes no body; one is a 422.
    """
    keys = delete_studio_run(
        session, workspace_id=workspace_id, project_id=project_id, run_id=run_id
    )
    for key in keys:
        assets.remove(key)


@router.get(
    "/media-assets",
    operation_id="listMediaAssets",
    response_model=MediaAssetListResponse,
    responses=problems(404),
)
def list_assets(
    project_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    before: CreatedBefore,
    limit: PageLimit = DEFAULT_PAGE_SIZE,
) -> MediaAssetListResponse:
    """The project's library: the assets its studio runs made, newest first, a page at a time."""
    page = list_media_assets(
        session, workspace_id=workspace_id, project_id=project_id, limit=limit, before=before
    )
    return MediaAssetListResponse(
        assets=[_asset_response(asset) for asset in page.items],
        next_cursor=created_cursor(page.next_before),
    )


@router.get(
    "/media-assets/{asset_id}",
    operation_id="getMediaAsset",
    response_model=MediaAssetDetailResponse,
    responses=problems(404),
)
def get_asset(
    project_id: uuid.UUID,
    asset_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
) -> MediaAssetDetailResponse:
    """One asset with its versions, each with how it was made and what its reviews found."""
    asset = media_asset(
        session, workspace_id=workspace_id, project_id=project_id, asset_id=asset_id
    )
    versions = asset_versions(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        asset_id=asset_id,
        limit=MAX_PAGE_SIZE,
    )
    return MediaAssetDetailResponse(
        asset=_asset_response(asset),
        versions=[_version_response(version) for version in versions.items],
    )


@router.post(
    "/media-assets/{asset_id}/accept",
    operation_id="acceptMediaAsset",
    response_model=MediaAssetResponse,
    responses=problems(404, 409),
    dependencies=[Depends(no_request_body)],
)
def accept(
    project_id: uuid.UUID,
    asset_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    decider: CurrentUserId,
) -> MediaAssetResponse:
    """Accept a draft asset, as its current version stands, as the current user.

    Accepting it again changes nothing. 409 `studio_run_unfinished` while its run may still
    add versions, and `media_asset_decided` when it was rejected. The request takes no body;
    one is a 422.
    """
    asset = accept_asset(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        asset_id=asset_id,
        decided_by=decider,
    )
    return _asset_response(asset)


@router.post(
    "/media-assets/{asset_id}/reject",
    operation_id="rejectMediaAsset",
    response_model=MediaAssetResponse,
    responses=problems(404, 409),
    dependencies=[Depends(no_request_body)],
)
def reject(
    project_id: uuid.UUID,
    asset_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    decider: CurrentUserId,
) -> MediaAssetResponse:
    """Reject a draft asset as the current user.

    Rejecting it again changes nothing. 409 `studio_run_unfinished` while its run may still
    add versions, and `media_asset_decided` when it was accepted. The request takes no body;
    one is a 422.
    """
    asset = reject_asset(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        asset_id=asset_id,
        decided_by=decider,
    )
    return _asset_response(asset)


@router.post(
    "/media-assets/{asset_id}/versions/{version_id}/select",
    operation_id="selectMediaAssetVersion",
    response_model=MediaAssetResponse,
    responses=problems(404, 409),
    dependencies=[Depends(no_request_body)],
)
def select_version(
    project_id: uuid.UUID,
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
) -> MediaAssetResponse:
    """Make one of the asset's versions the one it stands for.

    Choosing is not a decision: the asset stays a draft and another version can be chosen
    before it is accepted or rejected. 404 `media_asset_version_not_found` for a version that
    is not one of this asset's; 409 `studio_run_unfinished` while its run may still add
    versions, and `media_asset_decided` once it was accepted or rejected. The request takes no
    body; one is a 422.
    """
    asset = select_asset_version(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        asset_id=asset_id,
        version_id=version_id,
    )
    return _asset_response(asset)


_IMAGE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "The version's image, in the type stored with it.",
        "content": _IMAGE_CONTENT,
    },
    # 403 and 503 too: the workspace router would declare them in this route's media type.
    **json_problems(403, 404, 503),
}


@router.get(
    "/media-assets/{asset_id}/versions/{version_id}/image",
    operation_id="getMediaAssetImage",
    response_class=Response,
    responses=_IMAGE_RESPONSES,
)
def image(
    project_id: uuid.UUID,
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    assets: Assets,
) -> Response:
    """The image of one version, to show in the page.

    404 `media_asset_version_not_found` for a version that is not one of this asset's; 503
    `storage_unavailable` when object storage could not give it back.
    """
    return _image(session, workspace_id, project_id, asset_id, version_id, assets, "inline")


@router.get(
    "/media-assets/{asset_id}/versions/{version_id}/download",
    operation_id="downloadMediaAssetImage",
    response_class=Response,
    responses=_IMAGE_RESPONSES,
)
def download(
    project_id: uuid.UUID,
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    assets: Assets,
) -> Response:
    """The image of one version as a file to save, named after the version."""
    return _image(session, workspace_id, project_id, asset_id, version_id, assets, "attachment")


def _image(
    session: DbSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    assets: Assets,
    disposition: str,
) -> Response:
    version = asset_version(
        session, workspace_id=workspace_id, project_id=project_id, version_id=version_id
    )
    if version.asset_id != asset_id:
        raise MediaAssetVersionNotFound(project_id, version_id)
    body = assets.get(version.storage_key)
    filename = f"{version.id}.{EXTENSIONS[version.mime]}"
    return Response(
        content=body,
        media_type=version.mime,
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": IMAGE_CACHE_CONTROL,
        },
    )
