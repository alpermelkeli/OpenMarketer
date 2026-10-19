"""A person adding their own image to a project's repository images.

``add_repository_image`` prepares the upload (``uploads.py``), stores its bytes under a key of
its own in the project's person folder, and keeps its row (``db/repository_images.py``,
``keep_person_image``). The object is stored before the row so that no row names a missing
object; when the row is not kept (the project is gone, it has as many uploads as it may, or the
same bytes were uploaded already) the object is removed again. Choosing a kind and removing an
image need no object storage beyond the key the store answers, so they are the store's
functions.

It takes a session it does not commit and the object storage as arguments.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from openmarketer_core.db.repository_images import (
    NewRepositoryImage,
    RepositoryImage,
    keep_person_image,
    person_image_key,
)
from openmarketer_core.repository_analysis.repository_images.selection import (
    RepositoryImageKind,
)
from openmarketer_core.repository_analysis.repository_images.uploads import (
    MAX_PERSON_IMAGES,
    prepare_upload,
)
from openmarketer_core.studio.asset_store import AssetStore


def add_repository_image(
    session: Session,
    assets: AssetStore,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    name: str,
    data: bytes,
    kind: RepositoryImageKind,
    added_by: uuid.UUID,
) -> RepositoryImage:
    """Keep ``data`` as one of the project's images, of the kind the person gave it.

    The same bytes uploaded again answer the image kept already. Raises ``UploadTooLarge``,
    ``ImageRefused``, ``ProjectNotFound``, ``TooManyPersonImages`` and
    ``AssetStoreUnavailable``.
    """
    prepared = prepare_upload(name, data, kind)
    key = person_image_key(
        workspace_id=workspace_id,
        project_id=project_id,
        upload_id=uuid.uuid4(),
        mime=prepared.mime,
    )
    assets.put(key, prepared.data, content_type=prepared.mime)
    try:
        kept = keep_person_image(
            session,
            workspace_id=workspace_id,
            project_id=project_id,
            image=NewRepositoryImage(
                path=prepared.candidate.path,
                kind=kind,
                mime=prepared.mime,
                width=prepared.width,
                height=prepared.height,
                size_bytes=prepared.size_bytes,
                sha256=prepared.sha256,
                storage_key=key,
            ),
            added_by=added_by,
            most=MAX_PERSON_IMAGES,
        )
    except Exception:
        assets.remove(key)
        raise
    if not kept.added:
        assets.remove(key)
    return kept.image
