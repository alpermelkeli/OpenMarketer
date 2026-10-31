"""What the chat agent may read of a project: one port and its records, and no database.

The orchestrator's chat answers the operator's message about one project from what the
product has stored. This module says what the chat may ask for (``ChatReads``) and what it
is given (the frozen records below); ``reads_postgres.py`` answers from PostgreSQL.

Every method is about one workspace and one project, which the adapter holds: the graph
never names either, so it cannot read beyond the project its turn is about. A reading that
does not exist is ``None`` and is not an error; an identifier or a section that does not
exist is refused, because a person could not have typed it.

This module imports no session and reaches no database: only the vocabulary of what was
stored. The records are plain data, so a test can fake the port.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from openmarketer_core.agent_runtime.actions import ActionEffect
from openmarketer_core.agent_runtime.gate import ProposalStatus
from openmarketer_core.agent_runtime.model_input import shown
from openmarketer_core.db.models import (
    AnalysisRunStatus,
    ContentItemStatus,
    MediaAssetStatus,
    ProfileStatus,
    ProjectStatus,
    ResearchRunStatus,
    StudioRunStatus,
    WritingRunStatus,
)

# The sections of a Product Profile (the fields of ``profile.ProductProfile``), in order.
PROFILE_SECTIONS: tuple[str, ...] = (
    "product",
    "features",
    "brand",
    "audience",
    "business_model",
    "measurement",
)

# The most records a reading returns, however many are asked for.
MAX_LISTED_RECORDS = 20


class UnknownProfileSection(ValueError):
    """A section of the profile was asked for that the profile does not have."""

    def __init__(self, section: str) -> None:
        super().__init__(
            f"there is no section {shown(section)} in the Product Profile; the sections "
            f"are: {', '.join(PROFILE_SECTIONS)}"
        )
        self.section = section


@dataclass(frozen=True)
class PendingProposals:
    """The proposals of the project that wait for a person: how many, and the nearest deadline."""

    count: int
    next_expiry: datetime | None


@dataclass(frozen=True)
class ProfileReading:
    """One version of the project's Product Profile, without its whole content.

    ``content`` is the asked-for section as JSON data when a section was asked for, and
    ``None`` otherwise.
    """

    version: int
    status: ProfileStatus
    commit_sha: str | None
    edited_from_version: int | None
    created_at: datetime
    approved_at: datetime | None
    content: object | None


@dataclass(frozen=True)
class ProposalReading:
    """One action the policy gate held for a person: what was asked and where it stands."""

    id: uuid.UUID
    action_name: str
    effect: ActionEffect
    payload: dict[str, Any]
    status: ProposalStatus
    run_kind: str
    run_id: uuid.UUID
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None


@dataclass(frozen=True)
class RunReading:
    """One analysis run of the project and how far it got."""

    id: uuid.UUID
    status: AnalysisRunStatus
    error: str | None
    profile_version: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class ResearchRunReading:
    """One research run of the project and how far it got."""

    id: uuid.UUID
    status: ResearchRunStatus
    requested_by: str
    error: str | None
    report_id: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class ResearchReportReading:
    """A finished research run's report: its questions and the findings they got."""

    run_id: uuid.UUID
    requested_by: str
    questions: list[str]
    findings: list[dict[str, Any]]
    notes: list[str]
    cost_usd: float
    model: str
    created_at: datetime


@dataclass(frozen=True)
class AssetReviewReading:
    """What the checks and the visual verifier found of the version a studio run ended with.

    ``check_failures`` are the names of the programmatic checks it failed, worded by code;
    ``failed_criteria`` are the verifier's criteria it failed, each with the criterion, its
    severity and the verifier's note, which a model wrote. ``passed`` is ``None`` while the
    version has not been reviewed.
    """

    version_id: uuid.UUID
    passed: bool | None
    check_failures: list[str]
    failed_criteria: list[dict[str, Any]]


@dataclass(frozen=True)
class StudioRunReading:
    """One studio run of the project: what it was asked for and how far it got.

    ``goal`` and ``exact_copy`` are the request's, ``None`` and empty when the stored request
    no longer fits its schema. ``final_asset_id`` is the asset a run that succeeded or needs
    review ended with, in the project's Studio library; ``review`` is what the checks and the
    verifier found of it, and ``review_reason`` why a run that needs review was handed to a
    person, as the run's code worded it. Both are ``None`` where a listing leaves them out.
    """

    id: uuid.UUID
    status: StudioRunStatus
    platform: str
    goal: str | None
    exact_copy: list[str]
    requested_by: str
    error: str | None
    final_asset_id: uuid.UUID | None
    cost_usd: float
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    review: AssetReviewReading | None = None
    review_reason: str | None = None


@dataclass(frozen=True)
class MediaAssetReading:
    """One image of the project's Studio library and a person's decision on it."""

    id: uuid.UUID
    run_id: uuid.UUID
    platform: str
    status: MediaAssetStatus
    current_version_id: uuid.UUID | None
    created_at: datetime
    decided_at: datetime | None


@dataclass(frozen=True)
class WritingPostReading:
    """The post a writing run proposed, and where a person's decision on it stands.

    ``text`` is the post as it would be sent; ``passed_critic`` whether the critic passed it,
    ``None`` when the stored review does not say. ``proposal_id`` is the proposal a person
    decides it through, while the post waits for that decision, and ``None`` once it is
    settled (``status`` then says how). ``asset_id`` and ``asset_version_id`` are the image of
    the Studio library the post goes with, and the version of it the person approves with it.
    """

    id: uuid.UUID
    text: str
    status: ContentItemStatus
    passed_critic: bool | None
    proposal_id: uuid.UUID | None
    asset_id: uuid.UUID | None
    asset_version_id: uuid.UUID | None
    created_at: datetime
    settled_at: datetime | None


@dataclass(frozen=True)
class WritingRunReading:
    """One writing run of the project: the brief it was asked, and how far it got.

    ``brief`` is the stored brief as JSON data, ``None`` when it no longer fits its schema.
    ``post`` is the post the run proposed; ``None`` until it stored one, and where a listing
    leaves it out.
    """

    id: uuid.UUID
    status: WritingRunStatus
    platform: str
    brief: dict[str, Any] | None
    requested_by: str
    error: str | None
    profile_version: int | None
    cost_usd: float
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    post: WritingPostReading | None = None


@dataclass(frozen=True)
class ProjectStatusReading:
    """Where the project stands: what it is, its review state, and its latest work."""

    project_id: uuid.UUID
    name: str
    repository_url: str
    status: ProjectStatus
    autonomy_level: int
    product_url: str | None
    latest_approved_version: int | None
    latest_draft_version: int | None
    unfinished_run_id: uuid.UUID | None
    latest_run: RunReading | None
    pending: PendingProposals


class ChatReads(Protocol):
    """What the chat agent may read of its project, answered from where the product stores it."""

    def project_status(self) -> ProjectStatusReading:
        """The project with where its review stands and its latest run."""
        ...

    def profile(self, section: str | None = None) -> ProfileReading | None:
        """The latest approved profile, or the latest draft while nothing is approved.

        With ``section``, that section's content only. Raises ``UnknownProfileSection`` for a
        section the profile does not have.
        """
        ...

    def pending_proposals(self) -> Sequence[ProposalReading]:
        """The proposals that wait for a person, newest first, at most ``MAX_LISTED_RECORDS``."""
        ...

    def proposal(self, proposal_id: uuid.UUID) -> ProposalReading | None:
        """One proposal of the project, whatever it became, or ``None`` if there is none."""
        ...

    def run(self, run_id: uuid.UUID) -> RunReading | None:
        """One analysis run of the project, or ``None`` if there is none."""
        ...

    def recent_runs(self, limit: int) -> Sequence[RunReading]:
        """The project's analysis runs, newest first, at most ``limit`` records.

        ``MAX_LISTED_RECORDS`` bounds it further.
        """
        ...

    def recent_research(self, limit: int) -> Sequence[ResearchRunReading]:
        """The project's research runs, newest first, at most ``limit`` records.

        ``MAX_LISTED_RECORDS`` bounds it further.
        """
        ...

    def research_report(self, run_id: uuid.UUID | None = None) -> ResearchReportReading | None:
        """One research run's report, or the latest run's that has one, or ``None``.

        With ``run_id``, that run's report and nothing else: ``None`` when the run has
        none or the project has no such run.
        """
        ...

    def recent_studio_runs(self, limit: int) -> Sequence[StudioRunReading]:
        """The project's studio runs, newest first, at most ``limit`` records, without reviews.

        ``MAX_LISTED_RECORDS`` bounds it further.
        """
        ...

    def studio_run(self, run_id: uuid.UUID) -> StudioRunReading | None:
        """One studio run of the project with its review, or ``None`` if there is none."""
        ...

    def media_assets(self, limit: int) -> Sequence[MediaAssetReading]:
        """The project's Studio library, newest first, at most ``limit`` assets.

        ``MAX_LISTED_RECORDS`` bounds it further.
        """
        ...

    def recent_writing_runs(self, limit: int) -> Sequence[WritingRunReading]:
        """The project's writing runs, newest first, at most ``limit`` records, without posts.

        ``MAX_LISTED_RECORDS`` bounds it further.
        """
        ...

    def writing_run(self, run_id: uuid.UUID) -> WritingRunReading | None:
        """One writing run of the project with its post, or ``None`` if there is none."""
        ...
