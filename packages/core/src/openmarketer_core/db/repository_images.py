"""The product's own images, found in its repository by an analysis or uploaded by a person.

An analysis run collects the images (``repository_analysis/repository_images``) while it is
running, and the worker stores each one's bytes in object storage, under the run's own folder
(``repository_image_key``), before ``keep_repository_images`` writes their rows. A project
keeps the analysis images of one analysis: a run that kept any replaces those of every earlier
run of the project, and the function answers the storage keys of the rows it removed, so the
caller removes those objects once its transaction has committed. What a person decided about
a replaced image is carried over, by its hash, to the new run's image with the same bytes:
a kind a person chose stays chosen, and an image a person removed stays removed. Only a
running run keeps images (``AnalysisRunFinished``, ``AnalysisRunNotStarted`` otherwise), and a
project has one unfinished run at a time, so the run that keeps images is always its
project's latest. A run that has its images already keeps no second set, so a repeated attempt
changes nothing.

A person curates them: ``keep_person_image`` adds an upload, whose bytes the caller stored
under the project's ``person`` folder (``person_image_key``), at most ``most`` of them per
project and identical bytes once; ``set_repository_image_kind`` chooses an image's kind;
``remove_repository_image`` deletes an upload's row and answers its key, so the caller removes
the object, and only marks an analysis image removed, so that a later analysis remembers.
An analysis never replaces an upload.

``list_repository_images`` reads the project's images that are not removed, and
``repository_image`` one of them by its id. Both name its object's key; the bytes are read
from object storage by the caller. A removed image is treated as missing.

Every function is scoped to a workspace and a project, takes a session and never commits; an
image of another workspace or project is treated as missing. Writing takes the workspace lock;
keeping a run's images also holds the run's row. Images are also deleted with their project
(``db/project_removal.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, delete, func, select, tuple_, update
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
    RepositoryImageSource,
)
from openmarketer_core.db.pages import CreatedPosition, Page, page_of, rows_to_fetch
from openmarketer_core.db.projects import get_project
from openmarketer_core.db.workspace_lock import wait_for_other_writes
from openmarketer_core.repository_analysis.repository_images.selection import (
    RepositoryImageKind,
    StoredImageMime,
)

REPOSITORY_IMAGES_PREFIX = "repo-images"
# The folder of a project's uploads, beside those of its runs, whose names are UUIDs.
PERSON_FOLDER = "person"
EXTENSIONS: Mapping[StoredImageMime, str] = {"image/png": "png", "image/webp": "webp"}


# A stored type read back as the port's; the column's CHECK holds it to these.
_MIMES: Mapping[str, StoredImageMime] = {mime: mime for mime in EXTENSIONS}


class RepositoryImageNotFound(Exception):
    """The workspace has no such repository image of such a project, or it was removed."""

    def __init__(self, project_id: uuid.UUID, image_id: uuid.UUID) -> None:
        super().__init__(f"project {project_id} has no repository image {image_id}")


class TooManyPersonImages(Exception):
    """The project has as many uploaded images as it may keep."""

    def __init__(self, project_id: uuid.UUID, most: int) -> None:
        super().__init__(f"project {project_id} keeps at most {most} uploaded images")
        self.most = most


@dataclass(frozen=True)
class NewRepositoryImage:
    """An image whose bytes are stored, as its row is made.

    ``storage_key`` must be the run's own (``repository_image_key``) for an analysis image,
    the project's person folder (``person_image_key``) for an upload. An upload's ``path`` is
    the uploaded file's cleaned name.
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
    """One of the product's images: where it came from, what it is, its key.

    ``analysis_run_id`` is the analysis that found it, ``None`` for a person's upload.
    ``kind_chosen_by`` is the person who chose its kind, ``None`` while it is the path's hint.
    """

    id: uuid.UUID
    project_id: uuid.UUID
    source: RepositoryImageSource
    analysis_run_id: uuid.UUID | None
    path: str
    kind: RepositoryImageKind
    kind_chosen_by: uuid.UUID | None
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


@dataclass(frozen=True)
class PersonImageKept:
    """The upload's row, and whether it is new: ``False`` when the same bytes were kept already.

    When it is not new, the object just stored is named by no row and is the caller's to remove.
    """

    image: RepositoryImage
    added: bool


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


def person_image_key(
    *, workspace_id: uuid.UUID, project_id: uuid.UUID, upload_id: uuid.UUID, mime: StoredImageMime
) -> str:
    """Where one upload lives: named by an id of its own, so that no two uploads share one."""
    folder = _person_folder(workspace_id, project_id)
    return f"{folder}{upload_id}.{EXTENSIONS[mime]}"


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
    """Keep ``images`` as the project's analysis images, in place of those of earlier runs.

    A kind a person chose and a removal a person made are carried over to the image with the
    same bytes. Raises ``AnalysisRunNotFound``, ``AnalysisRunNotStarted`` and
    ``AnalysisRunFinished`` for a run that is not running, and ``ValueError`` for a key
    outside the run's folder.
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

    replaced = session.execute(
        delete(RepositoryImageRecord)
        .where(
            RepositoryImageRecord.project_id == project_id,
            RepositoryImageRecord.source == RepositoryImageSource.ANALYSIS,
        )
        .returning(
            RepositoryImageRecord.storage_key,
            RepositoryImageRecord.sha256,
            RepositoryImageRecord.kind,
            RepositoryImageRecord.kind_chosen_by,
            RepositoryImageRecord.removed_at,
            RepositoryImageRecord.removed_by,
        )
    ).all()
    decided = {row.sha256: row for row in replaced}
    records: list[RepositoryImageRecord] = []
    for image in images:
        record = RepositoryImageRecord(
            project_id=project_id,
            source=RepositoryImageSource.ANALYSIS,
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
        earlier = decided.get(image.sha256)
        if earlier is not None and earlier.kind_chosen_by is not None:
            record.kind = earlier.kind
            record.kind_chosen_by = earlier.kind_chosen_by
        if earlier is not None and earlier.removed_at is not None:
            record.removed_at = earlier.removed_at
            record.removed_by = earlier.removed_by
        records.append(record)
    session.add_all(records)
    session.flush()
    return ImagesKept(kept=True, keys_to_remove=tuple(row.storage_key for row in replaced))


def keep_person_image(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    image: NewRepositoryImage,
    added_by: uuid.UUID,
    most: int,
) -> PersonImageKept:
    """Keep a person's upload as one of the project's images, the kind they gave it chosen.

    The same bytes uploaded again answer the row kept already, unchanged. Raises
    ``ProjectNotFound``, ``TooManyPersonImages`` when the project has ``most`` uploads, and
    ``ValueError`` for a key outside the project's person folder.
    """
    wait_for_other_writes(session, workspace_id)
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    if not image.storage_key.startswith(_person_folder(workspace_id, project_id)):
        raise ValueError("an uploaded image is stored under its project's person folder")
    uploads = _images_of_project(workspace_id, project_id).where(
        RepositoryImageRecord.source == RepositoryImageSource.PERSON
    )
    same = session.scalar(uploads.where(RepositoryImageRecord.sha256 == image.sha256))
    if same is not None:
        return PersonImageKept(image=_image(same), added=False)
    count = session.scalar(select(func.count()).select_from(uploads.subquery())) or 0
    if count >= most:
        raise TooManyPersonImages(project_id, most)

    record = RepositoryImageRecord(
        project_id=project_id,
        source=RepositoryImageSource.PERSON,
        analysis_run_id=None,
        path=image.path,
        kind=image.kind,
        kind_chosen_by=added_by,
        mime=image.mime,
        width=image.width,
        height=image.height,
        size_bytes=image.size_bytes,
        sha256=image.sha256,
        storage_key=image.storage_key,
    )
    session.add(record)
    session.flush()
    return PersonImageKept(image=_image(record), added=True)


def set_repository_image_kind(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    image_id: uuid.UUID,
    kind: RepositoryImageKind,
    set_by: uuid.UUID,
) -> RepositoryImage:
    """Choose the image's kind, for this image and for the same bytes a later analysis finds.

    Raises ``RepositoryImageNotFound`` for an image that is not one of the project's kept ones.
    """
    wait_for_other_writes(session, workspace_id)
    record = _kept_image_to_change(session, workspace_id, project_id, image_id)
    record.kind = kind
    record.kind_chosen_by = set_by
    session.flush()
    return _image(record)


def remove_repository_image(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    image_id: uuid.UUID,
    removed_by: uuid.UUID,
) -> str | None:
    """Remove the image from the project's; answer the key of the object to remove, if any.

    An upload's row is deleted and its key answered. An analysis image is marked removed and
    keeps its object, so that a later analysis finding the same bytes keeps it removed; ``None``
    is answered then. Raises ``RepositoryImageNotFound`` for an image that is not one of the
    project's kept ones.
    """
    wait_for_other_writes(session, workspace_id)
    record = _kept_image_to_change(session, workspace_id, project_id, image_id)
    if record.source is RepositoryImageSource.PERSON:
        key = record.storage_key
        session.delete(record)
        session.flush()
        return key
    session.execute(
        update(RepositoryImageRecord)
        .where(RepositoryImageRecord.id == record.id)
        .values(removed_at=func.now(), removed_by=removed_by)
    )
    return None


def list_repository_images(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    limit: int,
    before: CreatedPosition | None = None,
) -> Page[RepositoryImage, CreatedPosition]:
    """The project's images that are not removed: its latest analysis's and its uploads.

    At most ``limit`` of them, newest first, starting after ``before`` (the ``next_before``
    of the previous page). Raises ``ProjectNotFound`` when the workspace has no such project.
    """
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    query = _kept_images_of_project(workspace_id, project_id)
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
    """One kept image of the project. Raises ``RepositoryImageNotFound``, for a removed one too."""
    record = session.scalar(
        _kept_images_of_project(workspace_id, project_id).where(
            RepositoryImageRecord.id == image_id
        )
    )
    if record is None:
        raise RepositoryImageNotFound(project_id, image_id)
    return _image(record)


def _run_folder(workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID) -> str:
    return f"{REPOSITORY_IMAGES_PREFIX}/{workspace_id}/{project_id}/{run_id}/"


def _person_folder(workspace_id: uuid.UUID, project_id: uuid.UUID) -> str:
    return f"{REPOSITORY_IMAGES_PREFIX}/{workspace_id}/{project_id}/{PERSON_FOLDER}/"


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


def _kept_image_to_change(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID, image_id: uuid.UUID
) -> RepositoryImageRecord:
    record = session.scalar(
        _kept_images_of_project(workspace_id, project_id)
        .where(RepositoryImageRecord.id == image_id)
        .with_for_update(of=RepositoryImageRecord)
        .execution_options(populate_existing=True)
    )
    if record is None:
        raise RepositoryImageNotFound(project_id, image_id)
    return record


def _images_of_project(
    workspace_id: uuid.UUID, project_id: uuid.UUID
) -> Select[RepositoryImageRecord]:
    return (
        select(RepositoryImageRecord)
        .join(Project, Project.id == RepositoryImageRecord.project_id)
        .where(Project.workspace_id == workspace_id, RepositoryImageRecord.project_id == project_id)
    )


def _kept_images_of_project(
    workspace_id: uuid.UUID, project_id: uuid.UUID
) -> Select[RepositoryImageRecord]:
    return _images_of_project(workspace_id, project_id).where(
        RepositoryImageRecord.removed_at.is_(None)
    )


def _image(record: RepositoryImageRecord) -> RepositoryImage:
    return RepositoryImage(
        id=record.id,
        project_id=record.project_id,
        source=record.source,
        analysis_run_id=record.analysis_run_id,
        path=record.path,
        kind=record.kind,
        kind_chosen_by=record.kind_chosen_by,
        mime=_MIMES[record.mime],
        width=record.width,
        height=record.height,
        size_bytes=record.size_bytes,
        sha256=record.sha256,
        storage_key=record.storage_key,
        created_at=record.created_at,
    )
