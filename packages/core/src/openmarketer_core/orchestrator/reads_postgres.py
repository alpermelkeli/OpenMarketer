"""``ChatReads`` answered from PostgreSQL: the adapter the chat's wiring builds per turn.

One adapter is built for one turn: a session and the workspace and project the turn is
about, so every reading is scoped to that project. It reads through the stores in ``db/``
and never writes, commits or opens a transaction of its own: the caller owns those.

This module imports SQLAlchemy and the stores; the graph reaches it only through the port.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from openmarketer_core.agent_runtime.gate import ProposalStatus
from openmarketer_core.db.analysis_runs import (
    AnalysisRun,
    AnalysisRunNotFound,
    analysis_run,
    list_analysis_runs,
)
from openmarketer_core.db.content_items import ContentItem, content_item
from openmarketer_core.db.media_assets import (
    MediaAsset,
    MediaAssetVersion,
    asset_version,
    list_media_assets,
    media_asset,
)
from openmarketer_core.db.models import ContentItemStatus, StudioRunStatus
from openmarketer_core.db.profile_versions import (
    ProfileVersion,
    latest_approved_profile,
    latest_draft_profile,
)
from openmarketer_core.db.projects import project_overview
from openmarketer_core.db.proposals import (
    ActionProposal,
    ProposalNotFound,
    get_proposal,
    list_proposals,
    pending_proposals_summary,
)
from openmarketer_core.db.research_runs import (
    ResearchRun,
    ResearchRunNotFound,
    list_research_runs,
)
from openmarketer_core.db.research_store import (
    ResearchReport,
    ResearchReportNotFound,
)
from openmarketer_core.db.research_store import (
    research_report as stored_research_report,
)
from openmarketer_core.db.run_events import latest_event_of_run
from openmarketer_core.db.studio_runs import (
    StudioRun,
    StudioRunNotFound,
    list_studio_runs,
    requested_asset,
)
from openmarketer_core.db.studio_runs import studio_run as stored_studio_run
from openmarketer_core.db.writing_runs import (
    WritingRun,
    WritingRunNotFound,
    list_writing_runs,
    requested_brief,
)
from openmarketer_core.db.writing_runs import writing_run as stored_writing_run
from openmarketer_core.orchestrator.reads import (
    MAX_LISTED_RECORDS,
    PROFILE_SECTIONS,
    AssetReviewReading,
    MediaAssetReading,
    PendingProposals,
    ProfileReading,
    ProjectStatusReading,
    ProposalReading,
    ResearchReportReading,
    ResearchRunReading,
    RunReading,
    StudioRunReading,
    UnknownProfileSection,
    WritingPostReading,
    WritingRunReading,
)
from openmarketer_core.run_events import ESCALATED
from openmarketer_core.studio.requests import AssetRequest, StoredAssetRequestInvalid
from openmarketer_core.studio.workflow_contract import STUDIO_RUN_KIND
from openmarketer_core.writing.brief import StoredWritingBriefInvalid
from openmarketer_core.writing.workflow_contract import WRITING_RUN_KIND
from openmarketer_core.writing.writer_agent.rules import critic_passes


@dataclass
class PostgresChatReads:
    """Reads of one project, from one session: built for one turn."""

    session: Session
    workspace_id: uuid.UUID
    project_id: uuid.UUID

    def project_status(self) -> ProjectStatusReading:
        overview = project_overview(
            self.session, workspace_id=self.workspace_id, project_id=self.project_id
        )
        latest = list_analysis_runs(
            self.session, workspace_id=self.workspace_id, project_id=self.project_id, limit=1
        )
        pending = pending_proposals_summary(
            self.session, workspace_id=self.workspace_id, project_id=self.project_id
        )
        return ProjectStatusReading(
            project_id=overview.project.id,
            name=overview.project.name,
            repository_url=overview.project.source_repo_url,
            status=overview.project.status,
            autonomy_level=overview.project.autonomy_level,
            product_url=overview.project.product_url,
            latest_approved_version=overview.latest_approved_version,
            latest_draft_version=overview.latest_draft_version,
            unfinished_run_id=overview.unfinished_run_id,
            latest_run=None if not latest.items else _run(latest.items[0]),
            pending=PendingProposals(count=pending.count, next_expiry=pending.next_expiry),
        )

    def profile(self, section: str | None = None) -> ProfileReading | None:
        if section is not None and section not in PROFILE_SECTIONS:
            raise UnknownProfileSection(section)
        stored = latest_approved_profile(
            self.session, workspace_id=self.workspace_id, project_id=self.project_id
        )
        if stored is None:
            stored = latest_draft_profile(
                self.session, workspace_id=self.workspace_id, project_id=self.project_id
            )
        if stored is None:
            return None
        return _profile(stored, section)

    def pending_proposals(self) -> Sequence[ProposalReading]:
        page = list_proposals(
            self.session,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            limit=MAX_LISTED_RECORDS,
            status=ProposalStatus.PENDING,
        )
        return [_proposal(item) for item in page.items]

    def proposal(self, proposal_id: uuid.UUID) -> ProposalReading | None:
        try:
            found = get_proposal(
                self.session,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                proposal_id=proposal_id,
            )
        except ProposalNotFound:
            return None
        return _proposal(found)

    def run(self, run_id: uuid.UUID) -> RunReading | None:
        try:
            found = analysis_run(
                self.session,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                run_id=run_id,
            )
        except AnalysisRunNotFound:
            return None
        return _run(found)

    def recent_runs(self, limit: int) -> Sequence[RunReading]:
        page = list_analysis_runs(
            self.session,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            limit=limit,
        )
        return [_run(item) for item in page.items]

    def recent_research(self, limit: int) -> Sequence[ResearchRunReading]:
        page = list_research_runs(
            self.session,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            limit=limit,
        )
        return [_research_run(item) for item in page.items]

    def research_report(self, run_id: uuid.UUID | None = None) -> ResearchReportReading | None:
        if run_id is None:
            runs = list_research_runs(
                self.session,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                limit=MAX_LISTED_RECORDS,
            ).items
            with_report = next((run for run in runs if run.report_id is not None), None)
            if with_report is None:
                return None
            run_id = with_report.id
        try:
            stored = stored_research_report(
                self.session,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                run_id=run_id,
            )
        except (ResearchRunNotFound, ResearchReportNotFound):
            return None
        return _research_report(stored)

    def recent_studio_runs(self, limit: int) -> Sequence[StudioRunReading]:
        page = list_studio_runs(
            self.session,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            limit=min(limit, MAX_LISTED_RECORDS),
        )
        return [studio_run_reading(run, self._request_of(run.id)) for run in page.items]

    def studio_run(self, run_id: uuid.UUID) -> StudioRunReading | None:
        try:
            run = stored_studio_run(
                self.session,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                run_id=run_id,
            )
        except StudioRunNotFound:
            return None
        return studio_run_reading(
            run,
            self._request_of(run.id),
            review=self._review_of(run),
            review_reason=self._review_reason_of(run),
        )

    def media_assets(self, limit: int) -> Sequence[MediaAssetReading]:
        page = list_media_assets(
            self.session,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            limit=min(limit, MAX_LISTED_RECORDS),
        )
        return [_media_asset(item) for item in page.items]

    def recent_writing_runs(self, limit: int) -> Sequence[WritingRunReading]:
        page = list_writing_runs(
            self.session,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            limit=min(limit, MAX_LISTED_RECORDS),
        )
        return [writing_run_reading(run, self._brief_of(run.id)) for run in page.items]

    def writing_run(self, run_id: uuid.UUID) -> WritingRunReading | None:
        try:
            run = stored_writing_run(
                self.session,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                run_id=run_id,
            )
        except WritingRunNotFound:
            return None
        return writing_run_reading(run, self._brief_of(run.id), post=self._post_of(run))

    def _brief_of(self, run_id: uuid.UUID) -> dict[str, Any] | None:
        try:
            brief = requested_brief(
                self.session,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                run_id=run_id,
            )
        except StoredWritingBriefInvalid:
            return None
        return brief.model_dump(mode="json")

    def _post_of(self, run: WritingRun) -> WritingPostReading | None:
        if run.content_item_id is None:
            return None
        item = content_item(
            self.session,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            item_id=run.content_item_id,
        )
        return _post(item, self._waiting_proposal_of(item))

    def _waiting_proposal_of(self, item: ContentItem) -> uuid.UUID | None:
        """The proposal a person decides the post through, while the post waits for it.

        The item names its proposal once the run knows it, which may be only when it is
        settled; until then the proposal is found among the project's pending ones.
        """
        if item.status is not ContentItemStatus.PROPOSED:
            return None
        if item.proposal_id is not None:
            return item.proposal_id
        pending = list_proposals(
            self.session,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            limit=MAX_LISTED_RECORDS,
            status=ProposalStatus.PENDING,
        )
        return next(
            (
                proposal.id
                for proposal in pending.items
                if proposal.run_kind == WRITING_RUN_KIND and proposal.run_id == item.run_id
            ),
            None,
        )

    def _request_of(self, run_id: uuid.UUID) -> AssetRequest | None:
        try:
            return requested_asset(
                self.session,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                run_id=run_id,
            )
        except StoredAssetRequestInvalid:
            return None

    def _review_of(self, run: StudioRun) -> AssetReviewReading | None:
        if run.final_asset_id is None:
            return None
        asset = media_asset(
            self.session,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            asset_id=run.final_asset_id,
        )
        if asset.current_version_id is None:
            return None
        version = asset_version(
            self.session,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            version_id=asset.current_version_id,
        )
        return _review(version)

    def _review_reason_of(self, run: StudioRun) -> str | None:
        if run.status is not StudioRunStatus.NEEDS_REVIEW:
            return None
        escalated = latest_event_of_run(
            self.session,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            run_kind=STUDIO_RUN_KIND,
            run_id=run.id,
            kind=ESCALATED,
        )
        reason = None if escalated is None else escalated.detail.get("reason")
        return reason if isinstance(reason, str) else None


def studio_run_reading(
    run: StudioRun,
    request: AssetRequest | None,
    *,
    review: AssetReviewReading | None = None,
    review_reason: str | None = None,
) -> StudioRunReading:
    """A studio run as the chat reads it, with the request it was stored with, if it fits."""
    return StudioRunReading(
        id=run.id,
        status=run.status,
        platform=run.platform.value,
        goal=None if request is None else request.goal,
        exact_copy=[] if request is None else list(request.exact_copy),
        requested_by=run.requested_by,
        error=run.error,
        final_asset_id=run.final_asset_id,
        cost_usd=float(run.cost_usd),
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        review=review,
        review_reason=review_reason,
    )


def writing_run_reading(
    run: WritingRun, brief: dict[str, Any] | None, *, post: WritingPostReading | None = None
) -> WritingRunReading:
    """A writing run as the chat reads it, with the brief it was stored with, if it fits."""
    return WritingRunReading(
        id=run.id,
        status=run.status,
        platform=run.platform.value,
        brief=brief,
        requested_by=run.requested_by,
        error=run.error,
        profile_version=run.profile_version,
        cost_usd=float(run.cost_usd),
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        post=post,
    )


def _post(item: ContentItem, waiting_proposal_id: uuid.UUID | None) -> WritingPostReading:
    return WritingPostReading(
        id=item.id,
        text=item.text,
        status=item.status,
        passed_critic=_passed_critic(item.critic_report),
        proposal_id=waiting_proposal_id,
        asset_id=item.asset_id,
        asset_version_id=item.asset_version_id,
        created_at=item.created_at,
        settled_at=item.settled_at,
    )


def _passed_critic(report: Mapping[str, Any]) -> bool | None:
    """Whether the critic passed the post, by the writer's own rule; ``None`` if unreadable.

    The report is JSON the worker stored; one that does not have the shape the rule reads is
    not a verdict either way.
    """
    try:
        return critic_passes(report)
    except (KeyError, TypeError):
        return None


def _review(version: MediaAssetVersion) -> AssetReviewReading:
    """The failures of a version's two reviews, read from what the studio stored."""
    check = version.check_report or {}
    failures = check.get("failures")
    return AssetReviewReading(
        version_id=version.id,
        passed=version.passed,
        check_failures=[f for f in failures if isinstance(f, str)]
        if isinstance(failures, list)
        else [],
        failed_criteria=_failed_criteria(version.verifier_report),
    )


def _failed_criteria(report: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    criteria = (report or {}).get("criteria")
    if not isinstance(criteria, list):
        return []
    return [
        {
            "criterion": item.get("criterion"),
            "severity": item.get("severity"),
            "note": item.get("note", ""),
        }
        for item in criteria
        if isinstance(item, dict) and item.get("passed") is False
    ]


def _media_asset(asset: MediaAsset) -> MediaAssetReading:
    return MediaAssetReading(
        id=asset.id,
        run_id=asset.run_id,
        platform=asset.platform.value,
        status=asset.status,
        current_version_id=asset.current_version_id,
        created_at=asset.created_at,
        decided_at=asset.decided_at,
    )


def _profile(stored: ProfileVersion, section: str | None) -> ProfileReading:
    content = stored.profile.model_dump(mode="json")
    return ProfileReading(
        version=stored.version,
        status=stored.status,
        commit_sha=stored.commit_sha,
        edited_from_version=stored.edited_from_version,
        created_at=stored.created_at,
        approved_at=stored.approved_at,
        content=None if section is None else content[section],
    )


def _proposal(proposal: ActionProposal) -> ProposalReading:
    return ProposalReading(
        id=proposal.id,
        action_name=proposal.action_name,
        effect=proposal.effect,
        payload=proposal.payload,
        status=proposal.status,
        run_kind=proposal.run_kind,
        run_id=proposal.run_id,
        created_at=proposal.created_at,
        expires_at=proposal.expires_at,
        decided_at=proposal.decided_at,
    )


def _run(run: AnalysisRun) -> RunReading:
    return RunReading(
        id=run.id,
        status=run.status,
        error=run.error,
        profile_version=run.profile_version,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _research_run(run: ResearchRun) -> ResearchRunReading:
    return ResearchRunReading(
        id=run.id,
        status=run.status,
        requested_by=run.requested_by,
        error=run.error,
        report_id=run.report_id,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _research_report(report: ResearchReport) -> ResearchReportReading:
    return ResearchReportReading(
        run_id=report.run_id,
        requested_by=report.requested_by,
        questions=report.questions,
        findings=report.findings,
        notes=report.notes,
        cost_usd=report.cost_usd,
        model=report.model,
        created_at=report.created_at,
    )
