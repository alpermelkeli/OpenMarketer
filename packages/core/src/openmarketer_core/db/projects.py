"""Project store: creating a project and reading it back inside its workspace.

A project is the repository of one product. Every function is scoped to a
workspace, takes a session and never commits: the caller owns the transaction.
Callers get a frozen ``StoredProject``, never the ORM row. Snapshots, evidence
and profile versions of a project are not handled here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from openmarketer_core.db.models import Project
from openmarketer_core.db.profile_versions import wait_for_other_writes
from openmarketer_core.intake import remote_repository_url


class ProjectNotFound(Exception):
    """The workspace has no project with that identifier."""


class ProjectAlreadyExists(Exception):
    """The workspace already has a project for that repository."""


@dataclass(frozen=True)
class StoredProject:
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    source_repo_url: str
    created_at: datetime


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
        raise ProjectAlreadyExists(f"a project for {repository_url} already exists")
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


def _stored(project: Project) -> StoredProject:
    return StoredProject(
        id=project.id,
        workspace_id=project.workspace_id,
        name=project.name,
        source_repo_url=project.source_repo_url,
        created_at=project.created_at,
    )
