"""The public pages a research run fetched: their rows, and when they expire.

The bytes of a page live in object storage (``research/page_store.py``); a row
here names them and keeps what the run knows of the page: its address after
redirects, its title, type and size, the hash of what was stored, and the
injection signs the code found in its text. A reading node reads the text back
through the row; a report cites pages by their id.

A run fetches one URL once, so the row for a page of a run is found before
anything is fetched again (``page_of_url``), and the unique constraint on
``(run_id, url)`` is what two attempts at the same fetch meet. Rows expire:
``expired_pages`` reads those fetched longer ago than the retention, and
``delete_pages`` removes the ones whose objects are gone. Expiry is
maintenance of the whole database and not scoped to a workspace, like the
checkpoint cleanups; every other function is scoped, takes a session and never
commits.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openmarketer_core.db.models import Project, ResearchPageRecord
from openmarketer_core.db.projects import get_project
from openmarketer_core.db.research_runs import (
    FINISHED,
    ResearchRunFinished,
    research_run,
)

# The name of the unique constraint two attempts at one fetch meet.
PAGE_URL_CONSTRAINT = "uq_research_page_run_id_url"


class ResearchPageNotFound(Exception):
    """The workspace has no such page of such a project."""

    def __init__(self, project_id: uuid.UUID, page_id: uuid.UUID) -> None:
        super().__init__(f"project {project_id} has no research page {page_id}")


@dataclass(frozen=True)
class NewPage:
    """A page that was fetched and stored, as its row is made."""

    url: str
    title: str
    content_type: str
    size_bytes: int
    storage_key: str
    sha256: str
    flags: tuple[str, ...]


@dataclass(frozen=True)
class ResearchPage:
    """One fetched page and everything the run knows of it, without its text."""

    id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    url: str
    title: str
    content_type: str
    size_bytes: int
    storage_key: str
    sha256: str
    flags: tuple[str, ...]
    fetched_at: datetime


def store_page(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    page_id: uuid.UUID,
    page: NewPage,
) -> ResearchPage:
    """Add the row of a fetched page of the run, unless its URL is already a page of it.

    ``page_id`` is made by the caller, because the object's key is built from
    it before anything is stored. The run must belong to the workspace and
    project and be unfinished; a page whose URL the run already has is returned
    as it is, so a repeated fetch stores one page. Raises ``ProjectNotFound``,
    ``ResearchRunNotFound``, ``ResearchRunFinished`` and
    ``StoredResearchRequestInvalid`` (from the run's request, which is read
    again, as everywhere a run is read).
    """
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    run = research_run(session, workspace_id=workspace_id, project_id=project_id, run_id=run_id)
    if run.status in FINISHED:
        raise ResearchRunFinished(run_id, run.status)
    existing = page_of_url(
        session, workspace_id=workspace_id, project_id=project_id, run_id=run_id, url=page.url
    )
    if existing is not None:
        return existing
    record = ResearchPageRecord(
        id=page_id,
        project_id=project_id,
        run_id=run_id,
        url=page.url,
        title=page.title,
        content_type=page.content_type,
        size_bytes=page.size_bytes,
        storage_key=page.storage_key,
        sha256=page.sha256,
        flags=list(page.flags),
    )
    try:
        # A savepoint, so that a refusal leaves the caller's transaction usable.
        with session.begin_nested():
            session.add(record)
            session.flush()
    except IntegrityError as e:
        if PAGE_URL_CONSTRAINT not in str(e.orig):
            raise
        found = page_of_url(
            session,
            workspace_id=workspace_id,
            project_id=project_id,
            run_id=run_id,
            url=page.url,
        )
        if found is None:
            raise
        return found
    return _page(record)


def page_of(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, page_id: uuid.UUID
) -> ResearchPage:
    """One fetched page of the project."""
    record = session.scalar(
        select(ResearchPageRecord)
        .join(Project, Project.id == ResearchPageRecord.project_id)
        .where(
            Project.workspace_id == workspace_id,
            ResearchPageRecord.project_id == project_id,
            ResearchPageRecord.id == page_id,
        )
    )
    if record is None:
        raise ResearchPageNotFound(project_id, page_id)
    return _page(record)


def page_of_url(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    url: str,
) -> ResearchPage | None:
    """The run's page at ``url``, or ``None`` when the run has none."""
    record = session.scalar(
        select(ResearchPageRecord)
        .join(Project, Project.id == ResearchPageRecord.project_id)
        .where(
            Project.workspace_id == workspace_id,
            ResearchPageRecord.project_id == project_id,
            ResearchPageRecord.run_id == run_id,
            ResearchPageRecord.url == url,
        )
    )
    return None if record is None else _page(record)


def expired_pages(
    session: Session, *, older_than: timedelta, limit: int
) -> tuple[ResearchPage, ...]:
    """The oldest pages fetched longer ago than ``older_than``, at most ``limit`` of them.

    This is maintenance of the whole database, not a caller's request: it is
    not scoped to a workspace, and it reads rows, not their objects. The worker
    removes the objects and then the rows whose objects are gone.
    """
    records = session.scalars(
        select(ResearchPageRecord)
        .where(ResearchPageRecord.fetched_at < func.now() - older_than)
        .order_by(ResearchPageRecord.fetched_at)
        .limit(limit)
    )
    return tuple(_page(record) for record in records)


def delete_pages(session: Session, page_ids: tuple[uuid.UUID, ...]) -> int:
    """Remove the rows of ``page_ids``; say how many rows went. Their objects are gone."""
    if not page_ids:
        return 0
    removed = session.scalars(
        delete(ResearchPageRecord)
        .where(ResearchPageRecord.id.in_(page_ids))
        .returning(ResearchPageRecord.id)
    ).all()
    return len(removed)


def _page(record: ResearchPageRecord) -> ResearchPage:
    return ResearchPage(
        id=record.id,
        project_id=record.project_id,
        run_id=record.run_id,
        url=record.url,
        title=record.title,
        content_type=record.content_type,
        size_bytes=record.size_bytes,
        storage_key=record.storage_key,
        sha256=record.sha256,
        flags=tuple(record.flags),
        fetched_at=record.fetched_at,
    )
