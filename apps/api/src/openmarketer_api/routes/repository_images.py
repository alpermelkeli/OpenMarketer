"""The product's own images an analysis found in the project's repository.

An analysis keeps the images the product shows of itself (app icon, logo, screenshots,
marketing images) as it runs; the project's list is those of its latest analysis that kept
any, newest first. Each says where it was in the repository and the kind its path suggests,
which is a hint from names the repository chose. An image is served from object storage with
the type stored with it, never one guessed from the bytes or the request, and only under the
project it belongs to; what is served is the image written again from its pixels, not the
repository's file.

Nothing here collects, changes or deletes an image: an analysis replaces them, and they go
with their project.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from openmarketer_api.dependencies import Assets, DbSession
from openmarketer_api.errors import json_problems, problems
from openmarketer_api.identity import CurrentWorkspaceId
from openmarketer_api.pagination import DEFAULT_PAGE_SIZE, CreatedBefore, PageLimit, created_cursor
from openmarketer_core.db.repository_images import (
    EXTENSIONS,
    RepositoryImage,
    list_repository_images,
    repository_image,
)
from openmarketer_core.repository_analysis.repository_images.selection import (
    RepositoryImageKind,
)

router = APIRouter(prefix="/projects/{project_id}", tags=["repository images"])

# An image never changes; it can only be replaced by a later analysis or deleted with its
# project, and then it has another id.
IMAGE_CACHE_CONTROL = "private, max-age=86400, immutable"


class RepositoryImageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    analysis_run_id: uuid.UUID = Field(description="The analysis that found the image.")
    path: str = Field(description="Where the image was in the repository, relative to its root.")
    kind: RepositoryImageKind = Field(
        description="What the image's path suggests it is; a hint, not a reading of the image."
    )
    mime: str = Field(description="`image/png` or `image/webp`.")
    width: int
    height: int
    size_bytes: int
    created_at: datetime


class RepositoryImageListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    images: list[RepositoryImageResponse]
    next_cursor: str | None = Field(
        description="Null on the last page; otherwise the `cursor` of the next request."
    )


def _image_response(image: RepositoryImage) -> RepositoryImageResponse:
    return RepositoryImageResponse(
        id=image.id,
        analysis_run_id=image.analysis_run_id,
        path=image.path,
        kind=image.kind,
        mime=image.mime,
        width=image.width,
        height=image.height,
        size_bytes=image.size_bytes,
        created_at=image.created_at,
    )


@router.get(
    "/repository-images",
    operation_id="listRepositoryImages",
    response_model=RepositoryImageListResponse,
    responses=problems(404),
)
def list_images(
    project_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    before: CreatedBefore,
    limit: PageLimit = DEFAULT_PAGE_SIZE,
) -> RepositoryImageListResponse:
    """The images of the project's latest analysis that found any, a page at a time."""
    page = list_repository_images(
        session, workspace_id=workspace_id, project_id=project_id, limit=limit, before=before
    )
    return RepositoryImageListResponse(
        images=[_image_response(image) for image in page.items],
        next_cursor=created_cursor(page.next_before),
    )


_IMAGE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "The image, in the type stored with it.",
        "content": {
            mime: {"schema": {"type": "string", "format": "binary"}} for mime in EXTENSIONS
        },
    },
    # 403 and 503 too: the workspace router would declare them in this route's media type.
    **json_problems(403, 404, 503),
}


@router.get(
    "/repository-images/{image_id}/image",
    operation_id="getRepositoryImage",
    response_class=Response,
    responses=_IMAGE_RESPONSES,
)
def image(
    project_id: uuid.UUID,
    image_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    assets: Assets,
) -> Response:
    """One image, to show in the page.

    404 `repository_image_not_found` for an image that is not one of this project's; 503
    `storage_unavailable` when object storage could not give it back.
    """
    found = repository_image(
        session, workspace_id=workspace_id, project_id=project_id, image_id=image_id
    )
    body = assets.get(found.storage_key)
    filename = f"{found.id}.{EXTENSIONS[found.mime]}"
    return Response(
        content=body,
        media_type=found.mime,
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": IMAGE_CACHE_CONTROL,
        },
    )
