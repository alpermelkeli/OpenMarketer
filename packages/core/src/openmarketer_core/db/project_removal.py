"""Deleting a project, and everything stored about it, for good.

A project is deleted with its analysis runs, its snapshots, the evidence found
in them, every Product Profile version, approved ones included, the proposals
of its runs, decided or expired, its chat threads with their turns and
messages, its research runs with their reports and the pages they fetched, its
studio runs with the media assets they made and every version of each, accepted
ones included, and the events its runs recorded. Nothing is kept to restore it from: deleting is
final, and a project made again for the same repository starts with no history.

A project with an unfinished run is not deleted (``ProjectHasUnfinishedRun``):
the worker that runs it would store a profile for a project that is gone, and
the workflow cannot be stopped from here. Nor is a project with an unfinished
research run (``ProjectHasUnfinishedResearch``) or an unfinished studio run
(``ProjectHasUnfinishedStudioRun``), for the same reason, nor one
with a pending proposal (``ProjectHasPendingProposal``): a run waits for a
person to decide it, and would find neither the proposal nor its project.
Deleting takes the workspace lock, as every write of a workspace does, so a run
cannot be stored in the middle of it.

Profile versions cannot be deleted with plain SQL; the trigger that guards
them makes one exception, a deletion made while the transaction names their
project in ``openmarketer.deleted_project``. This module is the only code that
sets it, for the statements that delete the versions and no longer.

The function answers the storage keys of the research pages and of the asset
versions' images it removed as rows; both live in the installation's one
bucket, and the caller removes each key's object from object storage before its
transaction commits, as deleting one research or studio run does.

What this does not do: it does not remove the project's entries of the budget
ledger (``db/budget_ledger.py``), which name it without a foreign key, so what
its runs spent still counts against the workspace's monthly cap. It does not
commit, does not touch the repository itself, does not stop workflows, and does
not remove the graph checkpoints of the project's runs. Those were forgotten
when each run ended, and what was missed is a thread of a run that no longer
exists, which the worker's periodic cleanup removes (``db/checkpoint_cleanup.py``).
Deleted rows stay in PostgreSQL's dead tuples until vacuum, in its write-ahead
log and in backups.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select, union_all
from sqlalchemy.orm import Session

from openmarketer_core.agent_runtime.gate import ProposalStatus
from openmarketer_core.db.analysis_runs import FINISHED as ANALYSIS_FINISHED
from openmarketer_core.db.models import (
    AnalysisRunRecord,
    ChatMessageRecord,
    ChatThreadRecord,
    ChatTurnRecord,
    Evidence,
    MediaAssetVersionRecord,
    ProductProfileRecord,
    Project,
    ProposalRecord,
    RepoSnapshot,
    ResearchPageRecord,
    ResearchReportRecord,
    ResearchRunRecord,
    RunEventRecord,
    StudioRunRecord,
)
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.research_runs import FINISHED as RESEARCH_FINISHED
from openmarketer_core.db.studio_runs import FINISHED as STUDIO_FINISHED
from openmarketer_core.db.studio_runs import remove_studio_rows
from openmarketer_core.db.workspace_lock import wait_for_other_writes

# Read by the trigger ``product_profile_guard_version``; the name is the migration's.
DELETED_PROJECT_SETTING = "openmarketer.deleted_project"


class ProjectHasUnfinishedRun(Exception):
    """The project has a run that is queued or running; it is deleted once the run has ended."""

    def __init__(self, project_id: uuid.UUID) -> None:
        super().__init__(
            f"project {project_id} has an analysis run in progress;"
            " it can be deleted when the run has finished"
        )


class ProjectHasUnfinishedResearch(Exception):
    """The project has a research run that is queued or running; it is deleted once the run ends."""

    def __init__(self, project_id: uuid.UUID) -> None:
        super().__init__(
            f"project {project_id} has a research run in progress;"
            " it can be deleted when the run has finished"
        )


class ProjectHasUnfinishedStudioRun(Exception):
    """The project has a studio run that is queued or running; it is deleted once the run ends."""

    def __init__(self, project_id: uuid.UUID) -> None:
        super().__init__(
            f"project {project_id} has a studio run in progress;"
            " it can be deleted when the run has finished"
        )


class ProjectHasPendingProposal(Exception):
    """A run of the project waits for a person to decide a proposal; it is deleted once decided."""

    def __init__(self, project_id: uuid.UUID) -> None:
        super().__init__(
            f"project {project_id} has a proposal waiting for a decision;"
            " it can be deleted when the proposal has been decided or has expired"
        )


def delete_project(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> tuple[str, ...]:
    """Delete the project and everything stored about it; answer its stored objects' keys.

    Raises ``ProjectNotFound`` when the workspace has no such project, and
    ``ProjectHasUnfinishedRun`` while one of its analysis runs is queued or running,
    ``ProjectHasUnfinishedResearch`` while one of its research runs is,
    ``ProjectHasUnfinishedStudioRun`` while one of its studio runs is, and
    ``ProjectHasPendingProposal`` while one of its proposals waits for a decision.
    """
    wait_for_other_writes(session, workspace_id)
    # Locked as well, so that a run requested meanwhile is either seen below or refused
    # afterwards by its foreign key, never left behind for a project that is gone.
    found = session.scalar(
        select(Project.id)
        .where(Project.id == project_id, Project.workspace_id == workspace_id)
        .with_for_update()
    )
    if found is None:
        raise ProjectNotFound(f"project {project_id} not found")
    if _has_unfinished_run(session, project_id):
        raise ProjectHasUnfinishedRun(project_id)
    if _has_unfinished_research(session, project_id):
        raise ProjectHasUnfinishedResearch(project_id)
    if _has_unfinished_studio_run(session, project_id):
        raise ProjectHasUnfinishedStudioRun(project_id)
    if _has_pending_proposal(session, project_id):
        raise ProjectHasPendingProposal(project_id)

    object_keys = tuple(
        session.scalars(
            union_all(
                select(ResearchPageRecord.storage_key).where(
                    ResearchPageRecord.project_id == project_id
                ),
                select(MediaAssetVersionRecord.storage_key).where(
                    MediaAssetVersionRecord.project_id == project_id
                ),
            )
        )
    )
    session.execute(delete(ProposalRecord).where(ProposalRecord.project_id == project_id))
    session.execute(delete(RunEventRecord).where(RunEventRecord.project_id == project_id))
    session.execute(delete(ChatMessageRecord).where(ChatMessageRecord.project_id == project_id))
    session.execute(delete(ChatTurnRecord).where(ChatTurnRecord.project_id == project_id))
    # The order the foreign keys allow: pages, then the runs, then the reports; a run
    # names the chat thread that asked for it, so the runs go before the threads.
    session.execute(delete(ResearchPageRecord).where(ResearchPageRecord.project_id == project_id))
    session.execute(delete(ResearchRunRecord).where(ResearchRunRecord.project_id == project_id))
    session.execute(
        delete(ResearchReportRecord).where(ResearchReportRecord.project_id == project_id)
    )
    remove_studio_rows(session, project_id=project_id)
    session.execute(delete(ChatThreadRecord).where(ChatThreadRecord.project_id == project_id))
    session.execute(delete(AnalysisRunRecord).where(AnalysisRunRecord.project_id == project_id))
    session.execute(delete(Evidence).where(Evidence.project_id == project_id))
    _delete_profile_versions(session, project_id)
    session.execute(delete(RepoSnapshot).where(RepoSnapshot.project_id == project_id))
    session.execute(delete(Project).where(Project.id == project_id))
    return object_keys


def _has_unfinished_run(session: Session, project_id: uuid.UUID) -> bool:
    unfinished = (
        select(AnalysisRunRecord.id)
        .where(
            AnalysisRunRecord.project_id == project_id,
            AnalysisRunRecord.status.not_in(ANALYSIS_FINISHED),
        )
        .exists()
    )
    return session.scalar(select(unfinished)) is True


def _has_unfinished_research(session: Session, project_id: uuid.UUID) -> bool:
    unfinished = (
        select(ResearchRunRecord.id)
        .where(
            ResearchRunRecord.project_id == project_id,
            ResearchRunRecord.status.not_in(RESEARCH_FINISHED),
        )
        .exists()
    )
    return session.scalar(select(unfinished)) is True


def _has_unfinished_studio_run(session: Session, project_id: uuid.UUID) -> bool:
    unfinished = (
        select(StudioRunRecord.id)
        .where(
            StudioRunRecord.project_id == project_id,
            StudioRunRecord.status.not_in(STUDIO_FINISHED),
        )
        .exists()
    )
    return session.scalar(select(unfinished)) is True


def _has_pending_proposal(session: Session, project_id: uuid.UUID) -> bool:
    pending = (
        select(ProposalRecord.id)
        .where(
            ProposalRecord.project_id == project_id,
            ProposalRecord.status == ProposalStatus.PENDING,
        )
        .exists()
    )
    return session.scalar(select(pending)) is True


def _delete_profile_versions(session: Session, project_id: uuid.UUID) -> None:
    """Delete the project's versions with the trigger's leave, and take the leave back."""
    _name_deleted_project(session, str(project_id))
    session.execute(
        delete(ProductProfileRecord).where(ProductProfileRecord.project_id == project_id)
    )
    _name_deleted_project(session, "")


def _name_deleted_project(session: Session, project_id: str) -> None:
    # Local to the transaction: it ends with it however it ends.
    session.execute(select(func.set_config(DELETED_PROJECT_SETTING, project_id, True)))
