"""Profile versions: the history of a project's Product Profile, and its approval.

A stored profile is never changed or deleted, so a version number is never
used twice. An analysis run (``evidence_store``) or an edit adds a version with
the next number; approval marks one draft with who approved it and when, and is
the only change the database accepts on a stored version.
Rows are converted to ``ProfileVersion`` here, so callers never see the ORM.
Where a version comes from travels with it as what a person can read: the
number of the version it was edited from and the commit that was analysed,
fetched by the statement that reads the version.

Every function is scoped to a workspace: a project of another workspace is
treated exactly like a project that does not exist. Functions take a session
and never commit: the caller owns the transaction.

``list_profile_versions`` returns ``ProfileVersionSummary`` rows and never
reads a profile's content, which can be large.

This module does not decide who may approve or which version may be approved or
edited. The approver is whatever user id the caller authenticated; there is no
user table to check it against.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, func, select, update
from sqlalchemy.orm import Session, aliased

from openmarketer_core.db.models import ProductProfileRecord, ProfileStatus, Project, RepoSnapshot
from openmarketer_core.db.pages import Page, page_of, rows_to_fetch
from openmarketer_core.db.projects import get_project
from openmarketer_core.db.workspace_lock import wait_for_other_writes
from openmarketer_core.profile import ProductProfile

_EditedVersion = aliased(ProductProfileRecord)


class ProfileVersionNotFound(Exception):
    """The workspace has no such version of such a project."""

    def __init__(self, project_id: uuid.UUID, version: int) -> None:
        super().__init__(f"project {project_id} has no profile version {version}")


class ProfileAlreadyApproved(Exception):
    """The version was approved before; an approval is recorded once."""

    def __init__(self, project_id: uuid.UUID, version: int) -> None:
        super().__init__(f"profile version {version} of project {project_id} is already approved")


@dataclass(frozen=True)
class ProfileVersion:
    """One stored version of a project's Product Profile and its review state.

    ``edited_from_version`` is ``None`` for a version stored by an analysis.
    ``commit_sha`` is the commit that was analysed; an edit keeps the commit of
    the version it was made from.
    """

    id: uuid.UUID
    project_id: uuid.UUID
    version: int
    status: ProfileStatus
    profile: ProductProfile
    snapshot_id: uuid.UUID | None
    commit_sha: str | None
    edited_from_id: uuid.UUID | None
    edited_from_version: int | None
    created_at: datetime
    approved_by: uuid.UUID | None
    approved_at: datetime | None


@dataclass(frozen=True)
class ProfileVersionSummary:
    """A stored version without its profile: what a history of versions shows."""

    project_id: uuid.UUID
    version: int
    status: ProfileStatus
    commit_sha: str | None
    edited_from_version: int | None
    created_at: datetime
    approved_by: uuid.UUID | None
    approved_at: datetime | None


@dataclass(frozen=True)
class _StoredVersion:
    record: ProductProfileRecord
    edited_from_version: int | None
    commit_sha: str | None


def save_edited_profile(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    edited_version: int,
    profile: ProductProfile,
) -> ProfileVersion:
    """Store ``profile`` as a new draft made by editing ``edited_version``.

    The edited version is left as it is, whether draft or approved. The new one
    takes the project's next version number and keeps the snapshot of the
    version it was edited from, because the edit is still about that commit.
    """
    wait_for_other_writes(session, workspace_id)
    edited = _stored_version(session, workspace_id, project_id, edited_version)
    record = ProductProfileRecord(
        project_id=project_id,
        snapshot_id=edited.record.snapshot_id,
        edited_from_id=edited.record.id,
        version=next_profile_version(session, project_id),
        content=profile.model_dump(mode="json"),
    )
    session.add(record)
    session.flush()
    return _profile_version(
        _StoredVersion(
            record, edited_from_version=edited.record.version, commit_sha=edited.commit_sha
        )
    )


def approve_profile_version(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    version: int,
    approved_by: uuid.UUID,
) -> ProfileVersion:
    """Record that the user ``approved_by`` approved ``version``, at the database's time."""
    wait_for_other_writes(session, workspace_id)
    stored = _stored_version(session, workspace_id, project_id, version)
    if stored.record.status is ProfileStatus.APPROVED:
        raise ProfileAlreadyApproved(project_id, version)
    session.execute(
        update(ProductProfileRecord)
        .where(ProductProfileRecord.id == stored.record.id)
        .values(status=ProfileStatus.APPROVED, approved_by=approved_by, approved_at=func.now())
    )
    session.refresh(stored.record)
    return _profile_version(stored)


def profile_version(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, version: int
) -> ProfileVersion:
    """The version with that number, or ``ProfileVersionNotFound``."""
    return _profile_version(_stored_version(session, workspace_id, project_id, version))


def list_profile_versions(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    limit: int,
    before_version: int | None = None,
) -> Page[ProfileVersionSummary, int]:
    """The project's versions without their profiles, highest number first.

    At most ``limit`` of them, starting below ``before_version`` (the
    ``next_before`` of the previous page) or at the highest. Raises
    ``ProjectNotFound`` when the workspace has no such project.
    """
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    query = _of_project(
        select(
            ProductProfileRecord.project_id,
            ProductProfileRecord.version,
            ProductProfileRecord.status,
            RepoSnapshot.commit_sha,
            _EditedVersion.version,
            ProductProfileRecord.created_at,
            ProductProfileRecord.approved_by,
            ProductProfileRecord.approved_at,
        ),
        workspace_id,
        project_id,
    )
    if before_version is not None:
        query = query.where(ProductProfileRecord.version < before_version)
    rows = session.execute(
        query.order_by(ProductProfileRecord.version.desc()).limit(rows_to_fetch(limit))
    )
    summaries = [ProfileVersionSummary(*row) for row in rows]
    return page_of(summaries, limit, lambda last: last.version)


def latest_approved_profile(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> ProfileVersion | None:
    """The approved version with the highest number, or ``None`` if nothing is approved.

    "Latest" follows the version number, not the time of approval: approving
    version 2 after version 3 leaves version 3 the latest approved profile.
    """
    return _latest_with_status(session, workspace_id, project_id, ProfileStatus.APPROVED)


def latest_draft_profile(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> ProfileVersion | None:
    """The draft with the highest number, or ``None`` if the project has no draft.

    Approved versions are ignored, so the draft returned may be older than the
    latest approved profile; a caller that wants only unreviewed newer work
    compares the two version numbers.
    """
    return _latest_with_status(session, workspace_id, project_id, ProfileStatus.DRAFT)


def next_profile_version(session: Session, project_id: uuid.UUID) -> int:
    """The number for the project's next version. Call ``wait_for_other_writes`` first."""
    latest = session.scalar(
        select(func.max(ProductProfileRecord.version)).where(
            ProductProfileRecord.project_id == project_id
        )
    )
    return (latest or 0) + 1


def _of_project[*Columns](
    versions: Select[*Columns], workspace_id: uuid.UUID, project_id: uuid.UUID
) -> Select[*Columns]:
    """``versions`` narrowed to one project of the workspace, with where each version comes from.

    The joins make ``_EditedVersion.version`` and ``RepoSnapshot.commit_sha``
    selectable; both are missing for a version that has no such origin.
    """
    return (
        versions.join(Project, Project.id == ProductProfileRecord.project_id)
        .outerjoin(_EditedVersion, _EditedVersion.id == ProductProfileRecord.edited_from_id)
        .outerjoin(RepoSnapshot, RepoSnapshot.id == ProductProfileRecord.snapshot_id)
        .where(Project.workspace_id == workspace_id, Project.id == project_id)
    )


def _versions_of_project(
    workspace_id: uuid.UUID, project_id: uuid.UUID
) -> Select[ProductProfileRecord, int, str]:
    return _of_project(
        select(ProductProfileRecord, _EditedVersion.version, RepoSnapshot.commit_sha),
        workspace_id,
        project_id,
    )


def _stored_version(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID, version: int
) -> _StoredVersion:
    row = session.execute(
        _versions_of_project(workspace_id, project_id)
        .where(ProductProfileRecord.version == version)
        # A row the session still holds from before it waited may have been approved since.
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise ProfileVersionNotFound(project_id, version)
    return _StoredVersion(*row)


def _latest_with_status(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID, status: ProfileStatus
) -> ProfileVersion | None:
    row = session.execute(
        _versions_of_project(workspace_id, project_id)
        .where(ProductProfileRecord.status == status)
        .order_by(ProductProfileRecord.version.desc())
        .limit(1)
    ).one_or_none()
    return None if row is None else _profile_version(_StoredVersion(*row))


def _profile_version(stored: _StoredVersion) -> ProfileVersion:
    record = stored.record
    return ProfileVersion(
        id=record.id,
        project_id=record.project_id,
        version=record.version,
        status=record.status,
        profile=ProductProfile.model_validate(record.content),
        snapshot_id=record.snapshot_id,
        commit_sha=stored.commit_sha,
        edited_from_id=record.edited_from_id,
        edited_from_version=stored.edited_from_version,
        created_at=record.created_at,
        approved_by=record.approved_by,
        approved_at=record.approved_at,
    )
