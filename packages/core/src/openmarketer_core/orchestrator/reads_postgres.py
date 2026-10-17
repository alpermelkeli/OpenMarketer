"""``ChatReads`` answered from PostgreSQL: the adapter the chat's wiring builds per turn.

One adapter is built for one turn: a session and the workspace and project the turn is
about, so every reading is scoped to that project. It reads through the stores in ``db/``
and never writes, commits or opens a transaction of its own: the caller owns those.

This module imports SQLAlchemy and the stores; the graph reaches it only through the port.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from openmarketer_core.agent_runtime.gate import ProposalStatus
from openmarketer_core.db.analysis_runs import (
    AnalysisRun,
    AnalysisRunNotFound,
    analysis_run,
    list_analysis_runs,
)
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
from openmarketer_core.orchestrator.reads import (
    MAX_LISTED_RECORDS,
    PROFILE_SECTIONS,
    PendingProposals,
    ProfileReading,
    ProjectStatusReading,
    ProposalReading,
    RunReading,
    UnknownProfileSection,
)


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
