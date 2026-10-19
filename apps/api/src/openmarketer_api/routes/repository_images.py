"""The product's own images: those an analysis found in the repository, and a person's uploads.

An analysis keeps the images the product shows of itself (app icon, logo, screenshots,
marketing images) as it runs; the project's list is those of its latest analysis that kept
any and the images people uploaded, without those a person removed, newest first. Each says
where it came from and its kind: a hint its path suggests, until a person chooses it. An image
is served from object storage with the type stored with it, never one guessed from the bytes
or the request, and only under the project it belongs to; what is served is the image written
again from its pixels, not the repository's or the person's file.

A person curates them: uploads an image (``POST``, as JSON with the bytes in base64, since
the dashboard's proxy forwards JSON only), chooses an image's kind (``PATCH``) and removes one
(``DELETE``). A later analysis keeps what a person decided about the images with the same
bytes, and never replaces an upload (``db/repository_images.py``). Who decided is the
requester (``identity.py``), never the body.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field

from openmarketer_api.dependencies import Assets, DbSession, no_request_body
from openmarketer_api.errors import json_problems, problems
from openmarketer_api.identity import CurrentUserId, CurrentWorkspaceId
from openmarketer_api.pagination import DEFAULT_PAGE_SIZE, CreatedBefore, PageLimit, created_cursor
from openmarketer_core.db.models import RepositoryImageSource
from openmarketer_core.db.repository_images import (
    EXTENSIONS,
    RepositoryImage,
    list_repository_images,
    remove_repository_image,
    repository_image,
    set_repository_image_kind,
)
from openmarketer_core.repository_analysis.repository_images.curation import (
    add_repository_image,
)
from openmarketer_core.repository_analysis.repository_images.preparation import ImageRefused
from openmarketer_core.repository_analysis.repository_images.selection import (
    RepositoryImageKind,
)
from openmarketer_core.repository_analysis.repository_images.uploads import (
    MAX_PERSON_IMAGES,
    MAX_UPLOAD_BYTES,
    UploadTooLarge,
)

router = APIRouter(prefix="/projects/{project_id}", tags=["repository images"])

# An image never changes; it can only be replaced by a later analysis or deleted with its
# project, and then it has another id.
IMAGE_CACHE_CONTROL = "private, max-age=86400, immutable"

# The longest base64 text of an upload of MAX_UPLOAD_BYTES, padding included.
MAX_UPLOAD_BASE64_LENGTH = -(-MAX_UPLOAD_BYTES // 3) * 4
# What a body of an upload may be in all, with its name and kind: what a proxy must let through.
UPLOAD_BODY_LIMIT_MB = 12


class RepositoryImageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    source: RepositoryImageSource = Field(
        description="`analysis` when an analysis found it, `person` when a person uploaded it."
    )
    analysis_run_id: uuid.UUID | None = Field(
        description="The analysis that found the image; null for an upload."
    )
    path: str = Field(
        description=(
            "Where the image was in the repository, relative to its root; for an upload, the"
            " uploaded file's name, cleaned."
        )
    )
    kind: RepositoryImageKind = Field(
        description=(
            "What the image is: the hint its path suggests, until a person chose it"
            " (`kind_chosen`)."
        )
    )
    kind_chosen: bool = Field(
        description="Whether a person chose the kind; always true for an upload."
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
        source=image.source,
        analysis_run_id=image.analysis_run_id,
        path=image.path,
        kind=image.kind,
        kind_chosen=image.kind_chosen_by is not None,
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
    """The project's images, a page at a time, newest first.

    Those of its latest analysis that found any and those people uploaded, without the ones a
    person removed.
    """
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


class RepositoryImageUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=1000,
        description="The file's name; its last part is kept, cleaned, at most 120 characters.",
    )
    kind: RepositoryImageKind
    data_base64: str = Field(
        description=(
            "The file's bytes in standard base64, without a `data:` prefix or line breaks:"
            f" at most {MAX_UPLOAD_BASE64_LENGTH} characters, {MAX_UPLOAD_BYTES} bytes decoded."
        )
    )


class RepositoryImageKindChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: RepositoryImageKind


@router.post(
    "/repository-images",
    operation_id="uploadRepositoryImage",
    status_code=201,
    response_model=RepositoryImageResponse,
    responses=problems(400, 404, 409, 413, 503),
    description=(
        "Upload one of the product's images, of the kind given. The body is JSON with the file"
        f" in base64: `data_base64` is at most {MAX_UPLOAD_BASE64_LENGTH} characters, the body"
        f" at most {UPLOAD_BODY_LIMIT_MB} MB, the file at most {MAX_UPLOAD_BYTES} bytes decoded."
        " It must be a PNG, JPEG or WebP image of one frame, at least 128 and at most 4096"
        " pixels a side; what is kept is written again from its pixels, without its metadata,"
        " as a PNG or a WebP. The same bytes uploaded again answer the image kept already."
        " 400 `repository_image_refused` for what is not such an image, or not base64, with the"
        " rule in the message; 413 `repository_image_too_large`; 409"
        f" `too_many_repository_images` when the project has {MAX_PERSON_IMAGES} uploads; 404"
        " `project_not_found`; 503 `storage_unavailable` when object storage could not keep it."
    ),
)
def upload(
    project_id: uuid.UUID,
    body: RepositoryImageUpload,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    user_id: CurrentUserId,
    assets: Assets,
) -> RepositoryImageResponse:
    """Upload one of the product's images, of the kind given."""
    if len(body.data_base64) > MAX_UPLOAD_BASE64_LENGTH:
        raise UploadTooLarge(len(body.data_base64) * 3 // 4)
    try:
        data = base64.b64decode(body.data_base64, validate=True)
    except (binascii.Error, ValueError):
        raise ImageRefused("the upload's data is not base64") from None
    image = add_repository_image(
        session,
        assets,
        workspace_id=workspace_id,
        project_id=project_id,
        name=body.name,
        data=data,
        kind=body.kind,
        added_by=user_id,
    )
    return _image_response(image)


@router.patch(
    "/repository-images/{image_id}",
    operation_id="setRepositoryImageKind",
    response_model=RepositoryImageResponse,
    responses=problems(404),
)
def set_kind(
    project_id: uuid.UUID,
    image_id: uuid.UUID,
    body: RepositoryImageKindChoice,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    user_id: CurrentUserId,
) -> RepositoryImageResponse:
    """Choose what an image is; a later analysis keeps the choice for the same bytes.

    404 `repository_image_not_found` for an image that is not one of this project's, or was
    removed.
    """
    image = set_repository_image_kind(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        image_id=image_id,
        kind=body.kind,
        set_by=user_id,
    )
    return _image_response(image)


@router.delete(
    "/repository-images/{image_id}",
    operation_id="removeRepositoryImage",
    status_code=204,
    responses=problems(404, 503),
    dependencies=[Depends(no_request_body)],
)
def remove(
    project_id: uuid.UUID,
    image_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    user_id: CurrentUserId,
    assets: Assets,
) -> None:
    """Remove an image from the project's.

    An upload is deleted with its file. An image an analysis found is only marked removed, so
    that a later analysis finding the same bytes leaves it out too. 404
    `repository_image_not_found` for an image that is not one of this project's, or was
    removed already; 503 `storage_unavailable` when an upload's file could not be removed, and
    the image stays then.
    """
    key = remove_repository_image(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        image_id=image_id,
        removed_by=user_id,
    )
    if key is not None:
        assets.remove(key)
