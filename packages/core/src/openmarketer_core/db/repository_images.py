"""The product's own images an analysis found in its repository, as rows.

An analysis run collects the images (``repository_analysis/repository_images``) while it is
running, and the worker stores each one's bytes in object storage, under the run's own folder
(``repository_image_key``), before ``keep_repository_images`` writes their rows. A project
keeps the images of one analysis: a run that kept any replaces those of every earlier run of
the project, and the function answers the storage keys of the rows it removed, so the caller
removes those objects once its transaction has committed. Only a running run keeps images
(``AnalysisRunFinished``, ``AnalysisRunNotStarted`` otherwise), and a project has one
unfinished run at a time, so the run that keeps images is always its project's latest. A run
that has its images already keeps no second set, so a repeated attempt changes nothing.

``list_repository_images`` reads the images of the project's latest run that kept any, and
``repository_image`` one image of the project by its id. Both name its object's key; the
bytes are read from object storage by the caller.

Every function is scoped to a workspace and a project, takes a session and never commits; an
image of another workspace or project is treated as missing. Writing takes the workspace lock
and holds the run's row. Images are also deleted with their project
(``db/project_removal.py``), never by a person on their own.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, delete, select, tuple_
from sqlalchemy.orm import Session

from openmarketer_core.db.analysis_runs import (
    AnalysisRunFinished,
    AnalysisRunNotFound,
    AnalysisRunNotStarted,
)
from openmarketer_core.db.models import (
    AnalysisRunRecord,
    AnalysisRunStatus,
    Project,
    RepositoryImageRecord,
)
from openmarketer_core.db.pages import CreatedPosition, Page, page_of, rows_to_fetch
from openmarketer_core.db.projects import get_project
from openmarketer_core.db.workspace_lock import wait_for_other_writes
from openmarketer_core.repository_analysis.repository_images.selection import (
    RepositoryImageKind,
    StoredImageMime,
)

REPOSITORY_IMAGES_PREFIX = "repo-images"
EXTENSIONS: Mapping[StoredImageMime, str] = {"image/png": "png", "image/webp": "webp"}


# A stored type read back as the port's; the column's CHECK holds it to these.
_MIMES: Mapping[str, StoredImageMime] = {mime: mime for mime in EXTENSIONS}


class RepositoryImageNotFound(Exception):
    """The workspace has no such repository image of such a project."""

    def __init__(self, project_id: uuid.UUID, image_id: uuid.UUID) -> None:
        super().__init__(f"project {project_id} has no repository image {image_id}")


@dataclass(frozen=True)
class NewRepositoryImage:
    """An image whose bytes are stored, as its row is made.

    ``storage_key`` must be the run's own (``repository_image_key``).
    """

    path: str
    kind: RepositoryImageKind
    mime: StoredImageMime
    width: int
    height: int
    size_bytes: int
    sha256: str
    storage_key: str


@dataclass(frozen=True)
class RepositoryImage:
    """One of the product's images: where it was in the repository, what it is, its key."""

    id: uuid.UUID
    project_id: uuid.UUID
    analysis_run_id: uuid.UUID
    path: str
    kind: RepositoryImageKind
    mime: StoredImageMime
    width: int
    height: int
    size_bytes: int
    sha256: str
    storage_key: str
    created_at: datetime


@dataclass(frozen=True)
class ImagesKept:
    """Whether the run's images were kept, and the stored objects no row names any more."""

    kept: bool
    keys_to_remove: tuple[str, ...] = ()


def repository_image_key(
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    analysis_run_id: uuid.UUID,
    sha256: str,
    mime: StoredImageMime,
) -> str:
    """Where an image of a run lives: named by its bytes, so a repeated attempt writes the same."""
    folder = _run_folder(workspace_id, project_id, analysis_run_id)
    return f"{folder}{sha256}.{EXTENSIONS[mime]}"


def run_has_repository_images(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, analysis_run_id: uuid.UUID
) -> bool:
    """Whether the run kept its images already."""
    found = session.scalar(
        _images_of_project(workspace_id, project_id)
        .where(RepositoryImageRecord.analysis_run_id == analysis_run_id)
        .limit(1)
    )
    return found is not None


def keep_repository_images(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    analysis_run_id: uuid.UUID,
    images: Sequence[NewRepositoryImage],
) -> ImagesKept:
    """Keep ``images`` as the project's, in place of those of earlier runs.

    Raises ``AnalysisRunNotFound``, ``AnalysisRunNotStarted`` and ``AnalysisRunFinished``
    for a run that is not running, and ``ValueError`` for a key outside the run's folder.
    """
    wait_for_other_writes(session, workspace_id)
    run = _running_run(session, workspace_id, project_id, analysis_run_id)
    folder = _run_folder(workspace_id, project_id, analysis_run_id)
    if any(not image.storage_key.startswith(folder) for image in images):
        raise ValueError("a repository image is stored under its run's own folder")
    if not images or run_has_repository_images(
        session, workspace_id=workspace_id, project_id=project_id, analysis_run_id=run.id
    ):
        return ImagesKept(kept=False)

    replaced = tuple(
        session.scalars(
            delete(RepositoryImageRecord)
            .where(RepositoryImageRecord.project_id == project_id)
            .returning(RepositoryImageRecord.storage_key)
        )
    )
    session.add_all(
        RepositoryImageRecord(
            project_id=project_id,
            analysis_run_id=run.id,
            path=image.path,
            kind=image.kind,
            mime=image.mime,
            width=image.width,
            height=image.height,
            size_bytes=image.size_bytes,
            sha256=image.sha256,
            storage_key=image.storage_key,
        )
        for image in images
    )
    session.flush()
    return ImagesKept(kept=True, keys_to_remove=replaced)


def list_repository_images(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    limit: int,
    before: CreatedPosition | None = None,
) -> Page[RepositoryImage, CreatedPosition]:
    """The images of the project's latest analysis that kept any.

    At most ``limit`` of them, newest first, starting after ``before`` (the ``next_before``
    of the previous page). Raises ``ProjectNotFound`` when the workspace has no such project.
    """
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    latest_run = (
        select(RepositoryImageRecord.analysis_run_id)
        .join(AnalysisRunRecord, AnalysisRunRecord.id == RepositoryImageRecord.analysis_run_id)
        .where(RepositoryImageRecord.project_id == project_id)
        .order_by(AnalysisRunRecord.created_at.desc(), AnalysisRunRecord.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    query = _images_of_project(workspace_id, project_id).where(
        RepositoryImageRecord.analysis_run_id == latest_run
    )
    if before is not None:
        query = query.where(
            tuple_(RepositoryImageRecord.created_at, RepositoryImageRecord.id)
            < tuple_(before.created_at, before.id)
        )
    rows = session.scalars(
        query.order_by(
            RepositoryImageRecord.created_at.desc(), RepositoryImageRecord.id.desc()
        ).limit(rows_to_fetch(limit))
    )
    images = [_image(record) for record in rows]
    return page_of(images, limit, lambda last: CreatedPosition(last.created_at, last.id))


def repository_image(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, image_id: uuid.UUID
) -> RepositoryImage:
    """One image of the project. Raises ``RepositoryImageNotFound``."""
    record = session.scalar(
        _images_of_project(workspace_id, project_id).where(RepositoryImageRecord.id == image_id)
    )
    if record is None:
        raise RepositoryImageNotFound(project_id, image_id)
    return _image(record)


def _run_folder(workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID) -> str:
    return f"{REPOSITORY_IMAGES_PREFIX}/{workspace_id}/{project_id}/{run_id}/"


def _running_run(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> AnalysisRunRecord:
    """The run, held until the transaction ends, so that it cannot end while images are kept."""
    record = session.scalar(
        select(AnalysisRunRecord)
        .join(Project, Project.id == AnalysisRunRecord.project_id)
        .where(
            Project.workspace_id == workspace_id,
            Project.id == project_id,
            AnalysisRunRecord.id == run_id,
        )
        .with_for_update(of=AnalysisRunRecord)
        .execution_options(populate_existing=True)
    )
    if record is None:
        raise AnalysisRunNotFound(project_id, run_id)
    if record.status is AnalysisRunStatus.QUEUED:
        raise AnalysisRunNotStarted(run_id)
    if record.status is not AnalysisRunStatus.RUNNING:
        raise AnalysisRunFinished(run_id, record.status)
    return record


def _images_of_project(
    workspace_id: uuid.UUID, project_id: uuid.UUID
) -> Select[RepositoryImageRecord]:
    return (
        select(RepositoryImageRecord)
        .join(Project, Project.id == RepositoryImageRecord.project_id)
        .where(Project.workspace_id == workspace_id, RepositoryImageRecord.project_id == project_id)
    )


def _image(record: RepositoryImageRecord) -> RepositoryImage:
    return RepositoryImage(
        id=record.id,
        project_id=record.project_id,
        analysis_run_id=record.analysis_run_id,
        path=record.path,
        kind=record.kind,
        mime=_MIMES[record.mime],
        width=record.width,
        height=record.height,
        size_bytes=record.size_bytes,
        sha256=record.sha256,
        storage_key=record.storage_key,
        created_at=record.created_at,
    )
