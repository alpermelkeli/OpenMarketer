"""Tables for projects, repository evidence and Product Profile versions.

Every table below ``workspace`` carries ``project_id`` so that row-level
security can bind a database session to one project.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from openmarketer_core.db.base import Base


class ProjectStatus(StrEnum):
    ONBOARDING = "onboarding"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class ProfileStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"


def _enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """Store an enum as text with a CHECK constraint (simpler to migrate than a native type)."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
    )


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _project_fk() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("project.id"), index=True)


# ---------------------------------------------------------------- project
class Workspace(Base):
    __tablename__ = "workspace"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    owner_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = _created_at()


class Project(Base):
    __tablename__ = "project"
    __table_args__ = (CheckConstraint("autonomy_level BETWEEN 0 AND 3", name="autonomy_level"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspace.id"), index=True)
    name: Mapped[str] = mapped_column(Text)
    source_repo_url: Mapped[str] = mapped_column(Text)
    product_url: Mapped[str | None] = mapped_column(Text)
    playbook_key: Mapped[str | None] = mapped_column(Text)
    autonomy_level: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    status: Mapped[ProjectStatus] = mapped_column(
        _enum(ProjectStatus, "status"), server_default=ProjectStatus.ONBOARDING.value
    )
    created_at: Mapped[datetime] = _created_at()


# --------------------------------------------------------------- evidence
class RepoSnapshot(Base):
    """The exact commit an analysis run looked at."""

    __tablename__ = "repo_snapshot"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    commit_sha: Mapped[str] = mapped_column(Text)
    ref: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class Evidence(Base):
    """One fact produced by a deterministic extractor, with where it was found."""

    __tablename__ = "evidence"
    __table_args__ = (
        CheckConstraint("start_line IS NULL OR start_line >= 1", name="start_line"),
        CheckConstraint(
            "end_line IS NULL OR (start_line IS NOT NULL AND end_line >= start_line)",
            name="end_line",
        ),
        Index("ix_evidence_snapshot_id_kind", "snapshot_id", "kind"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repo_snapshot.id"))
    extractor: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    value: Mapped[Any] = mapped_column(JSONB)
    file: Mapped[str] = mapped_column(Text)
    start_line: Mapped[int | None] = mapped_column(Integer)
    end_line: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()


# ---------------------------------------------------------------- profile
class ProductProfileRecord(Base):
    """One version of a project's Product Profile.

    ``content`` holds a serialised ``openmarketer_core.profile.ProductProfile``.
    A trigger written in the migration (``product_profile_guard_version``) lets
    a row be inserted only as a draft, lets an update change nothing but a
    draft's approval columns, and refuses every deletion.
    """

    __tablename__ = "product_profile"
    __table_args__ = (
        UniqueConstraint("project_id", "version"),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint(
            "(status = 'approved') = (approved_by IS NOT NULL)"
            " AND (status = 'approved') = (approved_at IS NOT NULL)",
            name="approval",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repo_snapshot.id"))
    edited_from_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("product_profile.id"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[ProfileStatus] = mapped_column(
        _enum(ProfileStatus, "status"), server_default=ProfileStatus.DRAFT.value
    )
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    approved_by: Mapped[uuid.UUID | None]
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
