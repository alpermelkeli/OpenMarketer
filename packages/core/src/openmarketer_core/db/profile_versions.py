"""Profile versions: the history of a project's Product Profile, and its approval.

A stored profile is never changed or deleted, so a version number is never
used twice. An analysis run (``evidence_store``) or an edit adds a version with
the next number; approval marks one draft with who approved it and when, and is
the only change the database accepts on a stored version.
Rows are converted to ``ProfileVersion`` here, so callers never see the ORM.

Every function is scoped to a workspace: a project of another workspace is
treated exactly like a project that does not exist. Functions take a session
and never commit: the caller owns the transaction.

This module does not decide who may approve or which version may be approved or
edited. The approver is whatever user id the caller authenticated; there is no
user table to check it against.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, func, select, update
from sqlalchemy.orm import Session

from openmarketer_core.db.models import ProductProfileRecord, ProfileStatus, Project, Workspace
from openmarketer_core.profile import ProductProfile


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
    """One stored version of a project's Product Profile and its review state."""

    id: uuid.UUID
    project_id: uuid.UUID
    version: int
    status: ProfileStatus
    profile: ProductProfile
    snapshot_id: uuid.UUID | None
    edited_from_id: uuid.UUID | None
    created_at: datetime
    approved_by: uuid.UUID | None
    approved_at: datetime | None


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
        snapshot_id=edited.snapshot_id,
        edited_from_id=edited.id,
        version=next_profile_version(session, project_id),
        content=profile.model_dump(mode="json"),
    )
    session.add(record)
    session.flush()
    return _profile_version(record)


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
    record = _stored_version(session, workspace_id, project_id, version)
    if record.status is ProfileStatus.APPROVED:
        raise ProfileAlreadyApproved(project_id, version)
    session.execute(
        update(ProductProfileRecord)
        .where(ProductProfileRecord.id == record.id)
        .values(status=ProfileStatus.APPROVED, approved_by=approved_by, approved_at=func.now())
    )
    session.refresh(record)
    return _profile_version(record)


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


def wait_for_other_writes(session: Session, workspace_id: uuid.UUID) -> None:
    """Hold the workspace row until the transaction ends.

    Writes in one workspace then run one after another, so two of them cannot
    create the same project, take the same version number or approve the same
    version. This relies on READ COMMITTED, PostgreSQL's default: under a
    stricter isolation level the writer that waited still sees the old rows and
    fails on the unique constraint instead of taking the next number.
    """
    session.execute(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update())


def next_profile_version(session: Session, project_id: uuid.UUID) -> int:
    """The number for the project's next version. Call ``wait_for_other_writes`` first."""
    latest = session.scalar(
        select(func.max(ProductProfileRecord.version)).where(
            ProductProfileRecord.project_id == project_id
        )
    )
    return (latest or 0) + 1


def _versions_of_project(
    workspace_id: uuid.UUID, project_id: uuid.UUID
) -> Select[ProductProfileRecord]:
    return (
        select(ProductProfileRecord)
        .join(Project, Project.id == ProductProfileRecord.project_id)
        .where(Project.workspace_id == workspace_id, Project.id == project_id)
    )


def _stored_version(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID, version: int
) -> ProductProfileRecord:
    record = session.scalar(
        _versions_of_project(workspace_id, project_id)
        .where(ProductProfileRecord.version == version)
        # A row the session still holds from before it waited may have been approved since.
        .execution_options(populate_existing=True)
    )
    if record is None:
        raise ProfileVersionNotFound(project_id, version)
    return record


def _latest_with_status(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID, status: ProfileStatus
) -> ProfileVersion | None:
    record = session.scalar(
        _versions_of_project(workspace_id, project_id)
        .where(ProductProfileRecord.status == status)
        .order_by(ProductProfileRecord.version.desc())
        .limit(1)
    )
    return None if record is None else _profile_version(record)


def _profile_version(record: ProductProfileRecord) -> ProfileVersion:
    return ProfileVersion(
        id=record.id,
        project_id=record.project_id,
        version=record.version,
        status=record.status,
        profile=ProductProfile.model_validate(record.content),
        snapshot_id=record.snapshot_id,
        edited_from_id=record.edited_from_id,
        created_at=record.created_at,
        approved_by=record.approved_by,
        approved_at=record.approved_at,
    )
