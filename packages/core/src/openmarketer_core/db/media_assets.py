"""Media assets: the images studio runs made, every version of each, and a person's decision.

An asset is one image a run made for its platform, as the library shows it; its versions are
the candidates of the run's rounds, each with the record of how it was made (technique,
provider, model, prompt, parameters, sandbox code, cost) and the two reviews of it: the
programmatic checks and the visual verifier. The image of a version is in object storage
(``studio/asset_store.py``) under the key its row names; nothing here reads or writes it.

The worker writes while its run is running. ``add_version`` stores a version, creating the
run's asset with its first version when no asset is named, and makes it the asset's current
version; it is keyed by the action whose handler stored the image, so an attempt that is tried
again finds the version it stored instead of adding a second. ``record_version_review`` adds
the reviews to a version once. Which version a run ends with is recorded with the run's
outcome (``db/studio_runs.py``).

A person decides on an asset once its run has ended: a draft is accepted or rejected, once,
and deciding the same again changes nothing (``MediaAssetDecided`` otherwise). A decision takes
the workspace lock like every person's write of a workspace.

Every function is scoped to a workspace and a project, takes a session and never commits; an
asset or version of another workspace or project is treated as missing. Assets are deleted
with their run or their project, never on their own (``db/studio_runs.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openmarketer_core.db.models import (
    AssetTechnique,
    MediaAssetRecord,
    MediaAssetStatus,
    MediaAssetVersionRecord,
    Project,
    StudioRunRecord,
    StudioRunStatus,
)
from openmarketer_core.db.pages import CreatedPosition, Page, page_of, rows_to_fetch
from openmarketer_core.db.projects import get_project
from openmarketer_core.db.studio_runs import (
    FINISHED,
    StudioRunFinished,
    StudioRunNotFound,
    StudioRunNotStarted,
    StudioRunUnfinished,
)
from openmarketer_core.db.workspace_lock import wait_for_other_writes
from openmarketer_core.studio.asset_store import ASSETS_PREFIX, EXTENSIONS
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.sandbox import ImageMime

# The name of the unique constraint two attempts at one action meet.
VERSION_ACTION_CONSTRAINT = "uq_media_asset_version_action_id"
# A stored type read back as the port's; the column's CHECK holds it to these.
_MIMES: Mapping[str, ImageMime] = {mime: mime for mime in EXTENSIONS}


class MediaAssetNotFound(Exception):
    """The workspace has no such asset of such a project (or of such a run)."""

    def __init__(self, project_id: uuid.UUID, asset_id: uuid.UUID) -> None:
        super().__init__(f"project {project_id} has no media asset {asset_id}")


class MediaAssetVersionNotFound(Exception):
    """The workspace has no such asset version of such a project."""

    def __init__(self, project_id: uuid.UUID, version_id: uuid.UUID) -> None:
        super().__init__(f"project {project_id} has no media asset version {version_id}")


class MediaAssetDecided(Exception):
    """A person decided on the asset before; the decision is recorded once."""

    def __init__(self, asset_id: uuid.UUID, status: MediaAssetStatus) -> None:
        self.status = status
        super().__init__(f"media asset {asset_id} has already been {status.value}")


class MediaAssetVersionReviewed(Exception):
    """The version's reviews were recorded before, with another outcome."""

    def __init__(self, version_id: uuid.UUID) -> None:
        super().__init__(f"media asset version {version_id} has been reviewed already")


@dataclass(frozen=True)
class NewAssetVersion:
    """A candidate whose image is stored, as its row is made.

    ``storage_key`` must be under the run's own folder (``asset_store.asset_key``).
    """

    round: int
    technique: AssetTechnique
    storage_key: str
    mime: ImageMime
    width: int
    height: int
    size_bytes: int
    cost_usd: Decimal
    provider: str | None = None
    model: str | None = None
    prompt: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    code: str | None = None


@dataclass(frozen=True)
class MediaAsset:
    """One image of a run as the library shows it, and a person's decision on it."""

    id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    platform: Platform
    status: MediaAssetStatus
    current_version_id: uuid.UUID | None
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class MediaAssetVersion:
    """One candidate of an asset with its provenance and its reviews; not its image."""

    id: uuid.UUID
    project_id: uuid.UUID
    asset_id: uuid.UUID
    action_id: str
    round: int
    technique: AssetTechnique
    storage_key: str
    mime: ImageMime
    width: int
    height: int
    size_bytes: int
    provider: str | None
    model: str | None
    prompt: str | None
    params: Mapping[str, Any]
    code: str | None
    check_report: Mapping[str, Any] | None
    verifier_report: Mapping[str, Any] | None
    passed: bool | None
    cost_usd: Decimal
    created_at: datetime


def add_version(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    version_id: uuid.UUID,
    action_id: str,
    version: NewAssetVersion,
    asset_id: uuid.UUID | None = None,
) -> MediaAssetVersion:
    """Store a version of the asset ``asset_id`` of the run, and make it the asset's current one.

    With no ``asset_id``, the version is the first of a new draft asset of the run, for the
    run's platform. ``version_id`` is made by the caller, because the image's key is built
    from it before anything is stored. A version already stored under ``action_id`` for the
    run is returned as it is, so an action tried again stores one version. The run must be
    running, and the asset one of its own: raises ``StudioRunNotFound``,
    ``StudioRunNotStarted``, ``StudioRunFinished``, ``MediaAssetNotFound``, and
    ``ValueError`` for a key outside the run's folder.
    """
    stored = _version_of_action(session, workspace_id, project_id, run_id, action_id)
    if stored is not None:
        return stored
    run = _running_run(session, workspace_id, project_id, run_id)
    folder = f"{ASSETS_PREFIX}/{workspace_id}/{project_id}/{run_id}/"
    if not version.storage_key.startswith(folder):
        raise ValueError("the image of a version is stored under its run's own folder")
    try:
        # A savepoint, so that a refusal leaves the caller's transaction usable.
        with session.begin_nested():
            asset = _asset_to_add_to(session, project_id, run, asset_id)
            record = _version_record(project_id, asset.id, version_id, action_id, version)
            session.add(record)
            session.flush()
            asset.current_version_id = record.id
            session.flush()
    except IntegrityError as e:
        if VERSION_ACTION_CONSTRAINT not in str(e.orig):
            raise
        found = _version_of_action(session, workspace_id, project_id, run_id, action_id)
        if found is None:
            raise
        return found
    session.refresh(record)
    return _version(record)


def record_version_review(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    version_id: uuid.UUID,
    check_report: Mapping[str, Any],
    verifier_report: Mapping[str, Any] | None,
    passed: bool,
) -> MediaAssetVersion:
    """Record what the checks and the verifier found of the version, and whether it passed.

    ``verifier_report`` is ``None`` when the verifier was not asked, as when the checks
    failed first. Recording the same reviews again changes nothing; other reviews of a
    reviewed version are ``MediaAssetVersionReviewed``. Raises ``MediaAssetVersionNotFound``.
    """
    record = session.scalar(
        _versions_of_project(workspace_id, project_id)
        .where(MediaAssetVersionRecord.id == version_id)
        .with_for_update(of=MediaAssetVersionRecord)
        .execution_options(populate_existing=True)
    )
    if record is None:
        raise MediaAssetVersionNotFound(project_id, version_id)
    reviews = (dict(check_report), None if verifier_report is None else dict(verifier_report))
    if record.passed is not None:
        if (record.check_report, record.verifier_report, record.passed) != (*reviews, passed):
            raise MediaAssetVersionReviewed(version_id)
        return _version(record)
    record.check_report, record.verifier_report = reviews
    record.passed = passed
    session.flush()
    return _version(record)


def cost_of_run_versions(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> Decimal:
    """What the stored versions of the run's assets cost together; 0 for a run with none.

    The paid operations of a run, as its handlers recorded them: what a run that failed is
    recorded to have spent (its model calls are counted in its graph's state, which a failed
    run does not hand back).
    """
    total = session.scalar(
        select(func.coalesce(func.sum(MediaAssetVersionRecord.cost_usd), 0))
        .join(MediaAssetRecord, MediaAssetRecord.id == MediaAssetVersionRecord.asset_id)
        .join(Project, Project.id == MediaAssetVersionRecord.project_id)
        .where(
            Project.workspace_id == workspace_id,
            MediaAssetVersionRecord.project_id == project_id,
            MediaAssetRecord.run_id == run_id,
        )
    )
    return Decimal(total or 0)


def media_asset(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, asset_id: uuid.UUID
) -> MediaAsset:
    """One asset of the project as it is now."""
    record = session.scalar(
        _assets_of_project(workspace_id, project_id).where(MediaAssetRecord.id == asset_id)
    )
    if record is None:
        raise MediaAssetNotFound(project_id, asset_id)
    return _asset(record)


def list_media_assets(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    limit: int,
    before: CreatedPosition | None = None,
) -> Page[MediaAsset, CreatedPosition]:
    """The project's library: its assets, newest first.

    At most ``limit`` of them, starting after ``before`` (the ``next_before`` of the previous
    page) or at the newest. Raises ``ProjectNotFound`` when the workspace has no such project.
    """
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    query = _assets_of_project(workspace_id, project_id)
    if before is not None:
        query = query.where(
            tuple_(MediaAssetRecord.created_at, MediaAssetRecord.id)
            < tuple_(before.created_at, before.id)
        )
    rows = session.scalars(
        query.order_by(MediaAssetRecord.created_at.desc(), MediaAssetRecord.id.desc()).limit(
            rows_to_fetch(limit)
        )
    )
    assets = [_asset(record) for record in rows]
    return page_of(assets, limit, lambda last: CreatedPosition(last.created_at, last.id))


def asset_versions(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    asset_id: uuid.UUID,
    limit: int,
    before: CreatedPosition | None = None,
) -> Page[MediaAssetVersion, CreatedPosition]:
    """The asset's versions, newest first, with their provenance and reviews.

    At most ``limit`` of them, starting after ``before`` or at the newest. Raises
    ``MediaAssetNotFound`` when the project has no such asset.
    """
    media_asset(session, workspace_id=workspace_id, project_id=project_id, asset_id=asset_id)
    query = _versions_of_project(workspace_id, project_id).where(
        MediaAssetVersionRecord.asset_id == asset_id
    )
    if before is not None:
        query = query.where(
            tuple_(MediaAssetVersionRecord.created_at, MediaAssetVersionRecord.id)
            < tuple_(before.created_at, before.id)
        )
    rows = session.scalars(
        query.order_by(
            MediaAssetVersionRecord.created_at.desc(), MediaAssetVersionRecord.id.desc()
        ).limit(rows_to_fetch(limit))
    )
    versions = [_version(record) for record in rows]
    return page_of(versions, limit, lambda last: CreatedPosition(last.created_at, last.id))


def asset_version(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, version_id: uuid.UUID
) -> MediaAssetVersion:
    """One version of an asset of the project: where its image is, and how it was made."""
    record = session.scalar(
        _versions_of_project(workspace_id, project_id).where(
            MediaAssetVersionRecord.id == version_id
        )
    )
    if record is None:
        raise MediaAssetVersionNotFound(project_id, version_id)
    return _version(record)


def accept_asset(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    asset_id: uuid.UUID,
    decided_by: uuid.UUID,
) -> MediaAsset:
    """A person accepts the draft asset, as its current version stands, once its run has ended.

    Accepting it again changes nothing. Raises ``MediaAssetNotFound``,
    ``StudioRunUnfinished`` while the run may still add versions, and ``MediaAssetDecided``
    for an asset that was rejected. Takes the workspace lock.
    """
    return _decide(
        session, workspace_id, project_id, asset_id, decided_by, MediaAssetStatus.ACCEPTED
    )


def reject_asset(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    asset_id: uuid.UUID,
    decided_by: uuid.UUID,
) -> MediaAsset:
    """A person rejects the draft asset once its run has ended.

    Rejecting it again changes nothing. Raises ``MediaAssetNotFound``,
    ``StudioRunUnfinished`` while the run may still add versions, and ``MediaAssetDecided``
    for an asset that was accepted. Takes the workspace lock.
    """
    return _decide(
        session, workspace_id, project_id, asset_id, decided_by, MediaAssetStatus.REJECTED
    )


def _decide(
    session: Session,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    asset_id: uuid.UUID,
    decided_by: uuid.UUID,
    decision: MediaAssetStatus,
) -> MediaAsset:
    wait_for_other_writes(session, workspace_id)
    found = session.execute(
        _assets_of_project(workspace_id, project_id)
        .add_columns(StudioRunRecord.status)
        .join(StudioRunRecord, StudioRunRecord.id == MediaAssetRecord.run_id)
        .where(MediaAssetRecord.id == asset_id)
        .with_for_update(of=MediaAssetRecord)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if found is None:
        raise MediaAssetNotFound(project_id, asset_id)
    record, run_status = found
    if record.status is decision:
        return _asset(record)
    if record.status is not MediaAssetStatus.DRAFT:
        raise MediaAssetDecided(asset_id, record.status)
    if run_status not in FINISHED:
        raise StudioRunUnfinished(record.run_id, run_status)
    session.execute(
        update(MediaAssetRecord)
        .where(MediaAssetRecord.id == record.id)
        .values(status=decision, decided_by=decided_by, decided_at=func.now())
    )
    session.refresh(record)
    return _asset(record)


def _running_run(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> StudioRunRecord:
    """The run, held until the transaction ends, so that it cannot end while a version is added."""
    record = session.scalar(
        select(StudioRunRecord)
        .join(Project, Project.id == StudioRunRecord.project_id)
        .where(
            Project.workspace_id == workspace_id,
            Project.id == project_id,
            StudioRunRecord.id == run_id,
        )
        .with_for_update(of=StudioRunRecord)
        .execution_options(populate_existing=True)
    )
    if record is None:
        raise StudioRunNotFound(project_id, run_id)
    if record.status is StudioRunStatus.QUEUED:
        raise StudioRunNotStarted(run_id)
    if record.status in FINISHED:
        raise StudioRunFinished(run_id, record.status)
    return record


def _asset_to_add_to(
    session: Session, project_id: uuid.UUID, run: StudioRunRecord, asset_id: uuid.UUID | None
) -> MediaAssetRecord:
    if asset_id is None:
        asset = MediaAssetRecord(project_id=project_id, run_id=run.id, platform=run.platform)
        session.add(asset)
        session.flush()
        return asset
    asset = session.scalar(
        select(MediaAssetRecord).where(
            MediaAssetRecord.id == asset_id,
            MediaAssetRecord.project_id == project_id,
            MediaAssetRecord.run_id == run.id,
        )
    )
    if asset is None:
        raise MediaAssetNotFound(project_id, asset_id)
    return asset


def _version_record(
    project_id: uuid.UUID,
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    action_id: str,
    version: NewAssetVersion,
) -> MediaAssetVersionRecord:
    return MediaAssetVersionRecord(
        id=version_id,
        project_id=project_id,
        asset_id=asset_id,
        action_id=action_id,
        round=version.round,
        technique=version.technique,
        storage_key=version.storage_key,
        mime=version.mime,
        width=version.width,
        height=version.height,
        size_bytes=version.size_bytes,
        provider=version.provider,
        model=version.model,
        prompt=version.prompt,
        params=dict(version.params),
        code=version.code,
        cost_usd=version.cost_usd,
    )


def _version_of_action(
    session: Session,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    action_id: str,
) -> MediaAssetVersion | None:
    record = session.scalar(
        _versions_of_project(workspace_id, project_id)
        .join(MediaAssetRecord, MediaAssetRecord.id == MediaAssetVersionRecord.asset_id)
        .where(
            MediaAssetRecord.run_id == run_id,
            MediaAssetVersionRecord.action_id == action_id,
        )
    )
    return None if record is None else _version(record)


def _assets_of_project(workspace_id: uuid.UUID, project_id: uuid.UUID) -> Select[MediaAssetRecord]:
    return (
        select(MediaAssetRecord)
        .join(Project, Project.id == MediaAssetRecord.project_id)
        .where(Project.workspace_id == workspace_id, MediaAssetRecord.project_id == project_id)
    )


def _versions_of_project(
    workspace_id: uuid.UUID, project_id: uuid.UUID
) -> Select[MediaAssetVersionRecord]:
    return (
        select(MediaAssetVersionRecord)
        .join(Project, Project.id == MediaAssetVersionRecord.project_id)
        .where(
            Project.workspace_id == workspace_id,
            MediaAssetVersionRecord.project_id == project_id,
        )
    )


def _asset(record: MediaAssetRecord) -> MediaAsset:
    return MediaAsset(
        id=record.id,
        project_id=record.project_id,
        run_id=record.run_id,
        platform=Platform(record.platform),
        status=record.status,
        current_version_id=record.current_version_id,
        decided_by=record.decided_by,
        decided_at=record.decided_at,
        created_at=record.created_at,
    )


def _version(record: MediaAssetVersionRecord) -> MediaAssetVersion:
    return MediaAssetVersion(
        id=record.id,
        project_id=record.project_id,
        asset_id=record.asset_id,
        action_id=record.action_id,
        round=record.round,
        technique=record.technique,
        storage_key=record.storage_key,
        mime=_MIMES[record.mime],
        width=record.width,
        height=record.height,
        size_bytes=record.size_bytes,
        provider=record.provider,
        model=record.model,
        prompt=record.prompt,
        params=dict(record.params),
        code=record.code,
        check_report=None if record.check_report is None else dict(record.check_report),
        verifier_report=None if record.verifier_report is None else dict(record.verifier_report),
        passed=record.passed,
        cost_usd=record.cost_usd,
        created_at=record.created_at,
    )
