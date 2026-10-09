"""Project store: creating a project, reading it back and listing a workspace's projects.

A project is the repository of one product. Every function is scoped to a
workspace, takes a session and never commits: the caller owns the transaction.
Callers get a frozen ``StoredProject`` or ``ProjectOverview``, never the ORM row.

An overview adds where the project's review stands: its latest draft, its
latest approved version and its unfinished run. Those are read here, in the
same statement as the project, so that a list costs one query; snapshots,
evidence, profile versions and runs themselves are not handled here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ScalarSelect, Select, func, select, tuple_
from sqlalchemy.orm import Session

from openmarketer_core.db.models import (
    AnalysisRunRecord,
    AnalysisRunStatus,
    ProductProfileRecord,
    ProfileStatus,
    Project,
)
from openmarketer_core.db.pages import CreatedPosition, Page, page_of, rows_to_fetch
from openmarketer_core.db.workspace_lock import wait_for_other_writes
from openmarketer_core.intake import remote_repository_url


class ProjectNotFound(Exception):
    """The workspace has no project with that identifier."""


class ProjectAlreadyExists(Exception):
    """The workspace already has a project for that repository: ``project_id``."""

    def __init__(self, repository_url: str, project_id: uuid.UUID) -> None:
        super().__init__(f"a project for {repository_url} already exists")
        self.project_id = project_id


@dataclass(frozen=True)
class StoredProject:
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    source_repo_url: str
    created_at: datetime


@dataclass(frozen=True)
class ProjectOverview:
    """A project and where its review stands.

    "Latest" follows the version number. The latest draft can be older than the
    latest approved version; a caller that wants only unreviewed newer work
    compares the two numbers.
    """

    project: StoredProject
    latest_draft_version: int | None
    latest_approved_version: int | None
    unfinished_run_id: uuid.UUID | None


def create_project(
    session: Session, *, workspace_id: uuid.UUID, name: str, source_repo_url: str
) -> StoredProject:
    """Add a project for a remote repository to the workspace.

    Raises ``IntakeError`` when the URL is not one a remote caller may submit,
    and ``ProjectAlreadyExists`` when the workspace has that repository already:
    an analysis run is stored under the project of its repository URL, so two
    projects for one URL could not be told apart.
    """
    repository_url = remote_repository_url(source_repo_url)
    wait_for_other_writes(session, workspace_id)
    taken = session.scalar(
        select(Project.id)
        .where(Project.workspace_id == workspace_id, Project.source_repo_url == repository_url)
        .limit(1)
    )
    if taken is not None:
        raise ProjectAlreadyExists(repository_url, taken)
    project = Project(workspace_id=workspace_id, name=name, source_repo_url=repository_url)
    session.add(project)
    session.flush()
    return _stored(project)


def get_project(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> StoredProject:
    """The project, if it belongs to the workspace; otherwise ``ProjectNotFound``."""
    project = session.scalar(
        select(Project).where(Project.id == project_id, Project.workspace_id == workspace_id)
    )
    if project is None:
        raise ProjectNotFound(f"project {project_id} not found")
    return _stored(project)


def project_overview(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> ProjectOverview:
    """The project with its review state, if it belongs to the workspace.

    Raises ``ProjectNotFound`` otherwise.
    """
    row = session.execute(
        _overviews_of_workspace(workspace_id).where(Project.id == project_id)
    ).one_or_none()
    if row is None:
        raise ProjectNotFound(f"project {project_id} not found")
    return _overview(*row)


def list_projects(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    limit: int,
    before: CreatedPosition | None = None,
) -> Page[ProjectOverview, CreatedPosition]:
    """The workspace's projects with their review state, newest first, in one query.

    At most ``limit`` of them, starting after ``before`` (the ``next_before``
    of the previous page) or at the newest.
    """
    query = _overviews_of_workspace(workspace_id)
    if before is not None:
        query = query.where(
            tuple_(Project.created_at, Project.id) < tuple_(before.created_at, before.id)
        )
    rows = session.execute(
        query.order_by(Project.created_at.desc(), Project.id.desc()).limit(rows_to_fetch(limit))
    )
    overviews = [_overview(*row) for row in rows]
    return page_of(
        overviews, limit, lambda last: CreatedPosition(last.project.created_at, last.project.id)
    )


def _overviews_of_workspace(
    workspace_id: uuid.UUID,
) -> Select[Project, int, int, uuid.UUID]:
    """Each project of the workspace with its review state; a state it lacks is NULL."""
    # The partial unique index allows a project one unfinished run, so the subquery has one row.
    unfinished_run = (
        select(AnalysisRunRecord.id)
        .where(
            AnalysisRunRecord.project_id == Project.id,
            AnalysisRunRecord.status.in_((AnalysisRunStatus.QUEUED, AnalysisRunStatus.RUNNING)),
        )
        .correlate(Project)
        .scalar_subquery()
    )
    return select(
        Project,
        _latest_version_with(ProfileStatus.DRAFT),
        _latest_version_with(ProfileStatus.APPROVED),
        unfinished_run,
    ).where(Project.workspace_id == workspace_id)


def _latest_version_with(status: ProfileStatus) -> ScalarSelect[int]:
    return (
        select(func.max(ProductProfileRecord.version))
        .where(ProductProfileRecord.project_id == Project.id, ProductProfileRecord.status == status)
        .correlate(Project)
        .scalar_subquery()
    )


def _overview(
    project: Project,
    latest_draft_version: int | None,
    latest_approved_version: int | None,
    unfinished_run_id: uuid.UUID | None,
) -> ProjectOverview:
    return ProjectOverview(
        project=_stored(project),
        latest_draft_version=latest_draft_version,
        latest_approved_version=latest_approved_version,
        unfinished_run_id=unfinished_run_id,
    )


def _stored(project: Project) -> StoredProject:
    return StoredProject(
        id=project.id,
        workspace_id=project.workspace_id,
        name=project.name,
        source_repo_url=project.source_repo_url,
        created_at=project.created_at,
    )
