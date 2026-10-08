"""Evidence store: what an analysis run leaves in the database.

One run adds a repository snapshot, the extractor facts found in it and a new
draft version of the project's Product Profile. The command line names the
project by its repository (``save_analysis``, which creates it on first use);
a caller that already has a project names it by id
(``save_analysis_of_project``). Domain objects (``Snapshot``, ``ExtractedFact``,
``ProductProfile``) are converted to rows here and nowhere else.

Functions take a session and never commit: the caller owns the transaction.
Every version is stored here as a draft; editing, approving and reading
versions is in ``profile_versions``, and the state of a requested run is in
``analysis_runs``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from openmarketer_core.db.models import (
    Evidence,
    ProductProfileRecord,
    Project,
    RepoSnapshot,
    Workspace,
)
from openmarketer_core.db.profile_versions import next_profile_version, wait_for_other_writes
from openmarketer_core.db.projects import get_project
from openmarketer_core.extraction import ExtractedFact
from openmarketer_core.intake import Snapshot
from openmarketer_core.profile import ProductProfile

LOCAL_WORKSPACE_NAME = "local"


@dataclass(frozen=True)
class SavedAnalysis:
    """Where one analysis run was stored."""

    project_id: uuid.UUID
    snapshot_id: uuid.UUID
    profile_id: uuid.UUID
    profile_version: int
    evidence_count: int


def local_workspace_id(session: Session) -> uuid.UUID:
    """The workspace of single-user local mode, created on first use."""
    existing = session.scalar(
        select(Workspace.id)
        .where(Workspace.name == LOCAL_WORKSPACE_NAME)
        .order_by(Workspace.created_at)
        .limit(1)
    )
    if existing is not None:
        return existing
    workspace = Workspace(name=LOCAL_WORKSPACE_NAME)
    session.add(workspace)
    session.flush()
    return workspace.id


def save_analysis(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    snapshot: Snapshot,
    facts: Iterable[ExtractedFact],
    profile: ProductProfile,
) -> SavedAnalysis:
    """Store one analysis run under the workspace's project for that repository.

    The first run of a repository creates the project, named after the product.
    Every run adds a snapshot, its evidence and the next profile version.
    """
    wait_for_other_writes(session, workspace_id)
    project = _project_for_repository(session, workspace_id, snapshot.source_url)
    if project is None:
        project = Project(
            workspace_id=workspace_id,
            name=profile.product.name,
            source_repo_url=snapshot.source_url,
        )
        session.add(project)
        session.flush()

    return _store_run(session, project.id, snapshot, facts, profile)


def save_analysis_of_project(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    snapshot: Snapshot,
    facts: Iterable[ExtractedFact],
    profile: ProductProfile,
) -> SavedAnalysis:
    """Store one analysis run under a project the workspace already has.

    Raises ``ProjectNotFound`` when the workspace has no such project. The
    project is taken as given: the repository URL of the snapshot is not
    compared with the project's.
    """
    wait_for_other_writes(session, workspace_id)
    project = get_project(session, workspace_id=workspace_id, project_id=project_id)
    return _store_run(session, project.id, snapshot, facts, profile)


def _store_run(
    session: Session,
    project_id: uuid.UUID,
    snapshot: Snapshot,
    facts: Iterable[ExtractedFact],
    profile: ProductProfile,
) -> SavedAnalysis:
    stored_snapshot = RepoSnapshot(
        project_id=project_id, commit_sha=snapshot.commit_sha, ref=snapshot.ref
    )
    session.add(stored_snapshot)
    session.flush()

    evidence_rows = [
        {
            "project_id": project_id,
            "snapshot_id": stored_snapshot.id,
            "extractor": fact.extractor,
            "kind": fact.kind,
            "value": fact.value,
            "file": fact.file,
            "start_line": fact.start_line,
            "end_line": fact.end_line,
        }
        for fact in facts
    ]
    if evidence_rows:
        session.execute(insert(Evidence), evidence_rows)

    record = ProductProfileRecord(
        project_id=project_id,
        snapshot_id=stored_snapshot.id,
        version=next_profile_version(session, project_id),
        content=profile.model_dump(mode="json"),
    )
    session.add(record)
    session.flush()

    return SavedAnalysis(
        project_id=project_id,
        snapshot_id=stored_snapshot.id,
        profile_id=record.id,
        profile_version=record.version,
        evidence_count=len(evidence_rows),
    )


def _project_for_repository(
    session: Session, workspace_id: uuid.UUID, source_repo_url: str
) -> Project | None:
    return session.scalar(
        select(Project)
        .where(Project.workspace_id == workspace_id, Project.source_repo_url == source_repo_url)
        .order_by(Project.created_at)
        .limit(1)
    )
