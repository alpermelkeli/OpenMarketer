"""Tables for projects, repository evidence, Product Profile versions, analysis runs, research
runs and their reports and pages, studio runs and the media assets they made, stored
credentials, the settings a workspace chose from the dashboard, the proposals of actions that
wait for a person, the operator's chat threads about a project with their turns, the events
runs record for the person watching them, and the budget ledger of what runs spend.

Every table below ``workspace`` carries ``project_id`` so that row-level
security can bind a database session to one project; ``credential`` and
``workspace_setting`` belong to the workspace itself and carry ``workspace_id``, and
``budget_ledger_entry`` carries both.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Numeric,
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
from openmarketer_core.budget.caps import SpendDecision
from openmarketer_core.db.base import Base
from openmarketer_core.research.questions import MAX_SOURCES
from openmarketer_core.studio.platforms import PLATFORMS


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


class StudioRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"  # its asset passed every check
    NEEDS_REVIEW = "needs_review"  # a limit was hit; its best candidate waits for a person
    FAILED = "failed"


class MediaAssetStatus(StrEnum):
    DRAFT = "draft"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class AssetTechnique(StrEnum):
    GENERATED = "generated"  # the image model made the whole image, its words included
    HTML_REFERENCE = "html_reference"  # a layout rendered from HTML, given to the image model
    BACKGROUND_OVERLAY = "background_overlay"  # a generated background, words laid over in code
    COMPOSED = "composed"  # made in code in the sandbox, without a generated image


# A failure message can carry git output or a traceback; the row keeps only its start.
ANALYSIS_ERROR_MAX_LENGTH = 2000
# The same for a research run, whose failures come from a model or a service and can carry
# a provider's answer.
RUN_ERROR_MAX_LENGTH = 2000
# And for a chat turn, whose failure is a sentence worded for the operator.
CHAT_TURN_ERROR_MAX_LENGTH = 2000


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
    # The action id (its idempotency key) of the run that asked for this one through an action
    # that starts a run; null for a run a person or the orchestrator's chat asked for. Unique,
    # so an attempt that is tried again finds the run it made instead of creating a second.
    origin_action_id: Mapped[str | None] = mapped_column(Text, unique=True)
    questions: Mapped[list[str]] = mapped_column(JSONB)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB)
    max_sources: Mapped[int] = mapped_column(Integer)
    # The chat thread whose turn asked for the run, which the run reports back to; null for a
    # run asked for anywhere else.
    chat_thread_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat_thread.id"), index=True
    )
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
    expire, or with their run when it is deleted, and the objects with them.
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


# ------------------------------------------------------------------ studio
_PLATFORM_SLUGS = ", ".join(f"'{platform.value}'" for platform in PLATFORMS)
_IMAGE_MIMES = "'image/png', 'image/jpeg', 'image/webp'"


class StudioRunRecord(Base):
    """One requested run of the creative studio, from the request to its outcome.

    Like research, a project may have several studio runs at once. The request (``request``,
    an ``AssetRequest`` as JSON) is stored with the run and checked again when the run starts;
    ``platform`` repeats the request's, for the lists. A run that succeeded, and one that hit a
    limit and waits for a person (``needs_review``), point at the asset they ended with, which
    must be one of the run's own. ``cost_usd`` is what the run spent in all, recorded when it
    ends.
    """

    __tablename__ = "studio_run"
    __table_args__ = (
        ForeignKeyConstraint(
            ["final_asset_id", "id"],
            ["media_asset.id", "media_asset.run_id"],
            # The asset names its run as well; the cycle is closed after both tables exist.
            use_alter=True,
        ),
        CheckConstraint(
            "(status IN ('succeeded', 'needs_review')) = (final_asset_id IS NOT NULL)",
            name="final_asset_id",
        ),
        CheckConstraint("(status = 'failed') = (error IS NOT NULL)", name="error"),
        CheckConstraint(f"char_length(error) <= {RUN_ERROR_MAX_LENGTH}", name="error_length"),
        # A run can fail before a worker picks it up, so a failed run may have no start.
        CheckConstraint(
            "status = 'failed' OR (status = 'queued') = (started_at IS NULL)", name="started_at"
        ),
        CheckConstraint(
            "(status IN ('succeeded', 'needs_review', 'failed')) = (finished_at IS NOT NULL)",
            name="finished_at",
        ),
        CheckConstraint("requested_by IN ('person', 'orchestrator', 'run')", name="requested_by"),
        CheckConstraint(f"platform IN ({_PLATFORM_SLUGS})", name="platform"),
        CheckConstraint("cost_usd >= 0", name="cost_usd"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    status: Mapped[StudioRunStatus] = mapped_column(
        _enum(StudioRunStatus, "status"), server_default=StudioRunStatus.QUEUED.value
    )
    error: Mapped[str | None] = mapped_column(Text)
    final_asset_id: Mapped[uuid.UUID | None]
    requested_by: Mapped[str] = mapped_column(Text, server_default="person")
    # The action id (its idempotency key) of the run that asked for this one through an action
    # that starts a run; null for a run a person or the orchestrator's chat asked for. Unique,
    # so an attempt that is tried again finds the run it made instead of creating a second.
    origin_action_id: Mapped[str | None] = mapped_column(Text, unique=True)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    platform: Mapped[str] = mapped_column(Text)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), server_default=text("0"))
    # The chat thread whose turn asked for the run, which the run reports back to; null for a
    # run asked for anywhere else.
    chat_thread_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat_thread.id"), index=True
    )
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MediaAssetRecord(Base):
    """One image a studio run made for a platform, and a person's decision on it.

    The asset is what the library shows; its versions (``media_asset_version``) are the
    candidates of its rounds, and ``current_version_id`` is the one it stands for now, which
    must be one of its own. An asset is a draft until a person accepts or rejects it, once.
    """

    __tablename__ = "media_asset"
    __table_args__ = (
        # Lets a studio run reference its final asset together with the run.
        UniqueConstraint("id", "run_id"),
        ForeignKeyConstraint(
            ["current_version_id", "id"],
            ["media_asset_version.id", "media_asset_version.asset_id"],
            # The version names its asset as well; the cycle is closed after both tables exist.
            use_alter=True,
        ),
        CheckConstraint(f"platform IN ({_PLATFORM_SLUGS})", name="platform"),
        CheckConstraint("(status = 'draft') = (decided_by IS NULL)", name="decided_by"),
        CheckConstraint("(status = 'draft') = (decided_at IS NULL)", name="decided_at"),
        CheckConstraint(
            "status = 'draft' OR current_version_id IS NOT NULL", name="current_version_id"
        ),
        # The library of a project reads newest first in this order.
        Index("ix_media_asset_project_id_created_at", "project_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("studio_run.id"), index=True)
    platform: Mapped[str] = mapped_column(Text)
    status: Mapped[MediaAssetStatus] = mapped_column(
        _enum(MediaAssetStatus, "status"), server_default=MediaAssetStatus.DRAFT.value
    )
    current_version_id: Mapped[uuid.UUID | None]
    decided_by: Mapped[uuid.UUID | None]
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class MediaAssetVersionRecord(Base):
    """One candidate of an asset, and the record of how it was made: its provenance.

    The image is in object storage under ``storage_key``; the row keeps its type and size,
    the technique, the provider, model, prompt, parameters and sandbox code that made it, what
    it cost, and the two reviews of it: the programmatic checks (``check_report``) and the
    visual verifier (``verifier_report``), with whether it passed. The reviews are recorded
    once, after the image (``passed`` is null until then). ``action_id`` is the idempotency
    key of the action whose handler stored the version, so an attempt of it that is tried
    again finds the version instead of storing a second.
    """

    __tablename__ = "media_asset_version"
    __table_args__ = (
        # Lets an asset reference its current version together with the asset.
        UniqueConstraint("id", "asset_id"),
        CheckConstraint("round >= 1", name="round"),
        CheckConstraint(f"mime IN ({_IMAGE_MIMES})", name="mime"),
        CheckConstraint("width > 0 AND height > 0", name="size"),
        CheckConstraint("size_bytes > 0", name="size_bytes"),
        CheckConstraint("cost_usd >= 0", name="cost_usd"),
        CheckConstraint("(passed IS NULL) = (check_report IS NULL)", name="check_report"),
        CheckConstraint("passed IS NOT NULL OR verifier_report IS NULL", name="verifier_report"),
        Index("ix_media_asset_version_asset_id_created_at", "asset_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("media_asset.id"))
    action_id: Mapped[str] = mapped_column(Text, unique=True)
    round: Mapped[int] = mapped_column(SmallInteger)
    technique: Mapped[AssetTechnique] = mapped_column(_enum(AssetTechnique, "technique"))
    storage_key: Mapped[str] = mapped_column(Text)
    mime: Mapped[str] = mapped_column(Text)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    size_bytes: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    prompt: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    code: Mapped[str | None] = mapped_column(Text)
    check_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    verifier_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    passed: Mapped[bool | None]
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    created_at: Mapped[datetime] = _created_at()


# -------------------------------------------------------------- proposals
class ProposalRecord(Base):
    """An action an agent asked for that the policy gate held for a person's decision.

    ``action_id`` is the action's idempotency key (``ActionRequest.action_id``), so a run that
    asks again for the same action finds the same proposal. ``workflow_id`` is the run's
    workflow, which is told when a person decides. The payload is what the agent asked to do,
    as the action's model read it: what the person approves. ``estimate_usd`` is what an action
    that spends was estimated to cost, the amount the budget ledger holds while a person
    decides; no other action has one.
    """

    __tablename__ = "proposal"
    __table_args__ = (
        CheckConstraint("effect <> 'internal'", name="effect_held"),
        CheckConstraint(
            "estimate_usd IS NULL OR (effect = 'spends' AND estimate_usd >= 0)",
            name="estimate_of_a_spend",
        ),
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
    estimate_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
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


class ChatTurnTrigger(StrEnum):
    MESSAGE = "message"  # the operator wrote a message
    RESEARCH_FINISHED = "research_finished"  # a research run the thread started has ended
    STUDIO_FINISHED = "studio_finished"  # a studio run the thread started has ended


class ChatTurnStatus(StrEnum):
    RUNNING = "running"
    ANSWERED = "answered"
    FAILED = "failed"


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

    A message is never changed once stored, and it is deleted only with its
    whole thread; the thread is the record of what was asked and answered.
    Which model answered, and what the turn cost, are not stored with it: the
    row is what a person reads.
    """

    __tablename__ = "chat_message"
    __table_args__ = (
        # The list of a thread's messages reads newest first in this order.
        Index("ix_chat_message_thread_id_created_at", "thread_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    thread_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_thread.id"))
    # The turn that wrote the message; null for a message stored before turns were.
    turn_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("chat_turn.id"), index=True)
    role: Mapped[ChatRole] = mapped_column(_enum(ChatRole, "role"))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class ChatTurnRecord(Base):
    """One turn of the orchestrator in a chat thread, from its start to its answer or failure.

    A turn answers the operator's message, or follows up on a research run or a studio run
    the thread started (``research_run_id`` or ``studio_run_id``, each set exactly for its
    trigger). A thread
    has at most one running turn; a failed turn keeps why, worded for the
    operator.
    """

    __tablename__ = "chat_turn"
    __table_args__ = (
        CheckConstraint(
            "(trigger = 'research_finished') = (research_run_id IS NOT NULL)",
            name="research_run_id",
        ),
        CheckConstraint(
            "(trigger = 'studio_finished') = (studio_run_id IS NOT NULL)",
            name="studio_run_id",
        ),
        CheckConstraint("(status = 'failed') = (error IS NOT NULL)", name="error"),
        CheckConstraint(f"char_length(error) <= {CHAT_TURN_ERROR_MAX_LENGTH}", name="error_length"),
        CheckConstraint("(status = 'running') = (finished_at IS NULL)", name="finished_at"),
        Index(
            "uq_chat_turn_thread_id_running",
            "thread_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    thread_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_thread.id"), index=True)
    trigger: Mapped[ChatTurnTrigger] = mapped_column(_enum(ChatTurnTrigger, "trigger"))
    # The finished run a follow-up is about, named without a foreign key, as run events
    # name their run: the run can be deleted on its own while the turn stays. One column for
    # each kind of run a chat can start, so that a CHECK can tie each to its trigger.
    research_run_id: Mapped[uuid.UUID | None] = mapped_column()
    studio_run_id: Mapped[uuid.UUID | None] = mapped_column()
    status: Mapped[ChatTurnStatus] = mapped_column(
        _enum(ChatTurnStatus, "status"), server_default=ChatTurnStatus.RUNNING.value
    )
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ----------------------------------------------------------------- events
class RunEventRecord(Base):
    """One event a run recorded for the person watching it (``openmarketer_core.run_events``).

    The identifier increases with every event and is the cursor a reader
    continues from. A run is named by its kind and id, as a proposal names it,
    without a foreign key: the events of every kind of run share the table.
    """

    __tablename__ = "run_event"
    __table_args__ = (Index("ix_run_event_run_kind_run_id_id", "run_kind", "run_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    project_id: Mapped[uuid.UUID] = _project_fk()
    run_kind: Mapped[str] = mapped_column(Text)
    run_id: Mapped[uuid.UUID]
    kind: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()


# ----------------------------------------------------------------- budget
class LedgerEntryKind(StrEnum):
    RESERVE = "reserve"  # an action's estimate, held before it runs
    COMMIT = "commit"  # what the action spent, once it ran
    RELEASE = "release"  # the hold given back: the action did not run


class BudgetLedgerEntryRecord(Base):
    """One entry of the ledger of what a workspace's runs spend (``openmarketer_core.budget``).

    The ledger only grows: an action's estimate is reserved once, and settled at most once, by
    a commit of what it spent or a release. Balances are derived by query, never stored. The
    trigger ``budget_ledger_entry_append_only`` (in the migration, not shown here) refuses an
    update or a delete, and a settling entry for an action that has no reservation in its
    workspace.

    The action is named by its run's kind and its idempotency key
    (``ActionRequest.action_id``), and the project and the run without a foreign key: what was
    spent stays counted against the workspace's cap when a project is deleted. ``decision`` is
    the one the reservation was made with, so that a retried reservation finds it again; a
    refused spend writes no entry.
    """

    __tablename__ = "budget_ledger_entry"
    __table_args__ = (
        CheckConstraint("amount_usd >= 0", name="amount_usd"),
        CheckConstraint(
            "(entry_kind = 'reserve') = (decision IS NOT NULL)", name="reserve_decision"
        ),
        CheckConstraint("decision <> 'refuse'", name="decision_held"),
        CheckConstraint("entry_kind <> 'release' OR amount_usd = 0", name="release_amount"),
        Index(
            "uq_budget_ledger_entry_reserve",
            "run_kind",
            "action_id",
            unique=True,
            postgresql_where=text("entry_kind = 'reserve'"),
        ),
        Index(
            "uq_budget_ledger_entry_settlement",
            "run_kind",
            "action_id",
            unique=True,
            postgresql_where=text("entry_kind IN ('commit', 'release')"),
        ),
        # The month's balance reads a workspace's reservations from the month's start.
        Index("ix_budget_ledger_entry_workspace_id_created_at", "workspace_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspace.id"))
    project_id: Mapped[uuid.UUID]
    run_kind: Mapped[str] = mapped_column(Text)
    run_id: Mapped[uuid.UUID]
    action_id: Mapped[str] = mapped_column(Text)
    entry_kind: Mapped[LedgerEntryKind] = mapped_column(_enum(LedgerEntryKind, "entry_kind"))
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    decision: Mapped[SpendDecision | None] = mapped_column(_enum(SpendDecision, "decision"))
    created_at: Mapped[datetime] = _created_at()
