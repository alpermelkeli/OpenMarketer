"""Deleting a project, and everything stored about it, for good.

A project is deleted with its analysis runs, its snapshots, the evidence found
in them and every Product Profile version, approved ones included. Nothing is
kept to restore it from: deleting is final, and a project made again for the
same repository starts with no history.

A project with an unfinished run is not deleted (``ProjectHasUnfinishedRun``):
the worker that runs it would store a profile for a project that is gone, and
the workflow cannot be stopped from here. Deleting takes the workspace lock, as
every write of a workspace does, so a run cannot be stored in the middle of it.

Profile versions cannot be deleted with plain SQL; the trigger that guards
them makes one exception, a deletion made while the transaction names their
project in ``openmarketer.deleted_project``. This module is the only code that
sets it, for the statements that delete the versions and no longer.

What this does not do: it does not commit, does not touch the repository
itself, does not stop workflows, and does not remove the graph checkpoints of
the project's runs. Those were forgotten when each run ended, and what was
missed is a thread of a run that no longer exists, which the worker's
periodic cleanup removes (``db/checkpoint_cleanup.py``). Deleted rows stay in
PostgreSQL's dead tuples until vacuum, in its write-ahead log and in backups.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from openmarketer_core.db.analysis_runs import FINISHED
from openmarketer_core.db.models import (
    AnalysisRunRecord,
    Evidence,
    ProductProfileRecord,
    Project,
    RepoSnapshot,
)
from openmarketer_core.db.projects import ProjectNotFound
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


def delete_project(session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID) -> None:
    """Delete the project and everything stored about it.

    Raises ``ProjectNotFound`` when the workspace has no such project, and
    ``ProjectHasUnfinishedRun`` while one of its runs is queued or running.
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

    session.execute(delete(AnalysisRunRecord).where(AnalysisRunRecord.project_id == project_id))
    session.execute(delete(Evidence).where(Evidence.project_id == project_id))
    _delete_profile_versions(session, project_id)
    session.execute(delete(RepoSnapshot).where(RepoSnapshot.project_id == project_id))
    session.execute(delete(Project).where(Project.id == project_id))


def _has_unfinished_run(session: Session, project_id: uuid.UUID) -> bool:
    unfinished = (
        select(AnalysisRunRecord.id)
        .where(
            AnalysisRunRecord.project_id == project_id,
            AnalysisRunRecord.status.not_in(FINISHED),
        )
        .exists()
    )
    return session.scalar(select(unfinished)) is True


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
