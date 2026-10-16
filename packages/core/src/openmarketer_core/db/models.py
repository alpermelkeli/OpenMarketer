"""Tables for projects, repository evidence, Product Profile versions, analysis runs, research
runs and their reports and pages, stored credentials, the settings a workspace chose from the
dashboard, the proposals of actions that wait for a person, and the operator's chat threads
about a project.

Every table below ``workspace`` carries ``project_id`` so that row-level
security can bind a database session to one project; ``credential`` and
``workspace_setting`` belong to the workspace itself and carry ``workspace_id``.
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
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from openmarketer_core.agent_runtime.actions import ActionEffect
from openmarketer_core.agent_runtime.gate import ProposalStatus
from openmarketer_core.db.base import Base
from openmarketer_core.research.questions import MAX_SOURCES


class ProjectStatus(StrEnum):
    ONBOARDING = "onboarding"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class ProfileStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"


class AnalysisRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ResearchRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# A failure message can carry git output or a traceback; the row keeps only its start.
ANALYSIS_ERROR_MAX_LENGTH = 2000
# The same for a research run, whose failures come from a model or a service and can carry
# a provider's answer.
RUN_ERROR_MAX_LENGTH = 2000


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


# ------------------------------------------------------------ credentials
class CredentialRecord(Base):
    """A secret of the workspace that was set from the dashboard, encrypted.

    ``name`` is a name of ``openmarketer_core.credentials.catalog``. The
    database does not check it: a name the code no longer knows is never read.
    """

    __tablename__ = "credential"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspace.id"), primary_key=True)
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    set_by: Mapped[uuid.UUID]
    set_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkspaceSettingRecord(Base):
    """One choice a workspace made in the dashboard's settings, as JSON.

    ``name`` says what was chosen (``model.tier.fast``, ``analysis.limits``); the
    names and the shape of each value are code (``workspace_settings/``), and a
    value is checked again when it is read. The database checks neither: a
    name the code no longer knows is never read.
    """

    __tablename__ = "workspace_setting"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspace.id"), primary_key=True)
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB)
    set_by: Mapped[uuid.UUID]
    set_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
        # Lets an analysis run reference a version together with its project.
        UniqueConstraint("id", "project_id"),
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


# ------------------------------------------------------------------- runs
class AnalysisRunRecord(Base):
    """One requested analysis of a project, from the request to its outcome.

    A project has at most one unfinished run. A succeeded run points at the
    profile version it stored, which must belong to the same project; the
    snapshot is that version's snapshot.
    """

    __tablename__ = "analysis_run"
    __table_args__ = (
        ForeignKeyConstraint(
            ["profile_id", "project_id"], ["product_profile.id", "product_profile.project_id"]
        ),
        CheckConstraint("(status = 'succeeded') = (profile_id IS NOT NULL)", name="profile_id"),
        CheckConstraint("(status = 'failed') = (error IS NOT NULL)", name="error"),
        CheckConstraint(f"char_length(error) <= {ANALYSIS_ERROR_MAX_LENGTH}", name="error_length"),
        # A run can fail before a worker picks it up, so a failed run may have no start.
        CheckConstraint(
            "status = 'failed' OR (status = 'queued') = (started_at IS NULL)", name="started_at"
        ),
        CheckConstraint(
            "(status IN ('succeeded', 'failed')) = (finished_at IS NOT NULL)", name="finished_at"
        ),
        Index(
            "uq_analysis_run_project_id_unfinished",
            "project_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    status: Mapped[AnalysisRunStatus] = mapped_column(
        _enum(AnalysisRunStatus, "status"), server_default=AnalysisRunStatus.QUEUED.value
    )
    error: Mapped[str | None] = mapped_column(Text)
    profile_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ----------------------------------------------------------------- research
class ResearchRunRecord(Base):
    """One requested research run of a project, from the request to its outcome.

    Unlike an analysis, a project may have several research runs at once: a run
    reads nothing of the project's own and pays for nothing but its model
    calls. The request (its questions, its scope and its page budget) is stored
    with the run and checked again when the run starts, and ``requested_by``
    says who asked: a person, the orchestrator's chat, or another run. A
    succeeded run points at the report it stored, which must belong to the same
    project.
    """

    __tablename__ = "research_run"
    __table_args__ = (
        ForeignKeyConstraint(
            ["report_id", "project_id"], ["research_report.id", "research_report.project_id"]
        ),
        CheckConstraint("(status = 'succeeded') = (report_id IS NOT NULL)", name="report_id"),
        CheckConstraint("(status = 'failed') = (error IS NOT NULL)", name="error"),
        CheckConstraint(f"char_length(error) <= {RUN_ERROR_MAX_LENGTH}", name="error_length"),
        # A run can fail before a worker picks it up, so a failed run may have no start.
        CheckConstraint(
            "status = 'failed' OR (status = 'queued') = (started_at IS NULL)", name="started_at"
        ),
        CheckConstraint(
            "(status IN ('succeeded', 'failed')) = (finished_at IS NOT NULL)", name="finished_at"
        ),
        CheckConstraint("requested_by IN ('person', 'orchestrator', 'run')", name="requested_by"),
        CheckConstraint(f"max_sources BETWEEN 1 AND {MAX_SOURCES}", name="max_sources"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    status: Mapped[ResearchRunStatus] = mapped_column(
        _enum(ResearchRunStatus, "status"), server_default=ResearchRunStatus.QUEUED.value
    )
    error: Mapped[str | None] = mapped_column(Text)
    report_id: Mapped[uuid.UUID | None]
    requested_by: Mapped[str] = mapped_column(Text, server_default="person")
    questions: Mapped[list[str]] = mapped_column(JSONB)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB)
    max_sources: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResearchReportRecord(Base):
    """What a finished research run produced: its findings with their sources, and its cost.

    ``findings`` holds checked findings as JSON (``CheckedFinding`` dumps): each
    names the pages it rests on and the quotes from them, so the report is the
    record that outlives the pages (``research_page`` is expired). ``notes``
    holds what the code dropped or worked around on the way.
    """

    __tablename__ = "research_report"
    __table_args__ = (
        # Lets a research run reference a report together with its project.
        UniqueConstraint("id", "project_id"),
        CheckConstraint("cost_usd >= 0", name="cost_usd"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    questions: Mapped[list[str]] = mapped_column(JSONB)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    notes: Mapped[list[str]] = mapped_column(JSONB)
    cost_usd: Mapped[float] = mapped_column(Float)
    model: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class ResearchPageRecord(Base):
    """One public page a research run fetched, and where its stored text is.

    The bytes live in object storage under ``storage_key``; the row names them
    and keeps what the run knows of the page: its address (after redirects),
    its title, type and size, the hash of what was stored, and the injection
    signs the code found in its text. A run fetches one URL once: the unique
    constraint is what a repeated fetch of the same page of the same run finds
    instead of fetching and storing it again. Rows are removed when they
    expire, and the objects with them.
    """

    __tablename__ = "research_page"
    __table_args__ = (
        UniqueConstraint("run_id", "url", name="uq_research_page_run_id_url"),
        Index("ix_research_page_fetched_at", "fetched_at"),
        CheckConstraint("size_bytes >= 0", name="size_bytes"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("research_run.id"))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, server_default="")
    content_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text)
    flags: Mapped[list[str]] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# -------------------------------------------------------------- proposals
class ProposalRecord(Base):
    """An action an agent asked for that the policy gate held for a person's decision.

    ``action_id`` is the action's idempotency key (``ActionRequest.action_id``), so a run that
    asks again for the same action finds the same proposal. ``workflow_id`` is the run's
    workflow, which is told when a person decides. The payload is what the agent asked to do,
    as the action's model read it: what the person approves.
    """

    __tablename__ = "proposal"
    __table_args__ = (
        CheckConstraint("effect <> 'internal'", name="effect_held"),
        CheckConstraint(
            "(status IN ('approved', 'rejected')) = (decided_by IS NOT NULL)", name="decided_by"
        ),
        CheckConstraint("(status = 'pending') = (decided_at IS NULL)", name="decided_at"),
        Index("ix_proposal_project_id_status", "project_id", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    run_kind: Mapped[str] = mapped_column(Text)
    run_id: Mapped[uuid.UUID]
    workflow_id: Mapped[str] = mapped_column(Text)
    action_id: Mapped[str] = mapped_column(Text, unique=True)
    action_name: Mapped[str] = mapped_column(Text)
    effect: Mapped[ActionEffect] = mapped_column(_enum(ActionEffect, "effect"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[ProposalStatus] = mapped_column(
        _enum(ProposalStatus, "status"), server_default=ProposalStatus.PENDING.value
    )
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[uuid.UUID | None]
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ------------------------------------------------------------------- chat
class ChatRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ChatThreadRecord(Base):
    """One named conversation of the operator's chat about a project.

    A thread is opened empty and gathers the exchanges of one conversation.
    ``last_activity_at`` orders the project's threads, newest first;
    ``last_read_at`` is how far the operator has read, so a message after it
    leaves the thread unread.
    """

    __tablename__ = "chat_thread"
    __table_args__ = (
        # The list of a project's threads reads most active first in this order.
        Index(
            "ix_chat_thread_project_id_last_activity_at",
            "project_id",
            "last_activity_at",
            "id",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    title: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatMessageRecord(Base):
    """One message of a project's chat thread, as it was written.

    A message is never changed or deleted once stored; the thread is the record
    of what was asked and answered. Which model answered, and what the turn
    cost, are not stored with it: the row is what a person reads.
    """

    __tablename__ = "chat_message"
    __table_args__ = (
        # The list of a thread's messages reads newest first in this order.
        Index("ix_chat_message_thread_id_created_at", "thread_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    thread_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_thread.id"))
    role: Mapped[ChatRole] = mapped_column(_enum(ChatRole, "role"))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
