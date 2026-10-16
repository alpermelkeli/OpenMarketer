"""Research runs: the state of each requested research of a project.

The API requests a run and later asks for its status; the worker reports that
it started and how it ended. The row is the only shared record of that, so a
run's state survives a restart of either. Unlike an analysis, a project may
have any number of research runs at once: a run reads nothing of the project's
own and pays for nothing but its model calls, so a second request does not
make the first one pay twice.

A run moves forwards only:

- requested: ``queued``;
- ``queued`` to ``running`` when the worker picks it up. Reporting the start of
  a run that is already running changes nothing, because a retried activity
  reports it again;
- ``running`` to ``succeeded``, with the report the run stored. Reporting the
  same success again changes nothing;
- ``queued`` or ``running`` to ``failed``, with a message. A run can fail
  before it starts, for instance when its workflow cannot be started.
  Reporting a failure of a run that already failed changes nothing and keeps
  the first message.

Everything else is refused: a finished run is never started, failed or given
another result (``ResearchRunFinished``), and a run that was never started
cannot succeed (``ResearchRunNotStarted``).

The request is stored with the run and read back strictly whenever the run is
executed (``run_to_execute``): a stored request that no longer fits its schema
is ``StoredResearchRequestInvalid``, and it ends that run with its words.

Every function but one is scoped to a workspace and a project, including those
the worker calls: a run of another workspace or project is treated exactly like
a run that does not exist. The one is ``run_to_execute``, which is how the
worker learns the scope of a run it was handed by its id. Functions take a
session and never commit.

This module does not start or cancel workflows, and stores no workflow id: the
caller derives it from the run id (``research/workflow_contract.py``). The
thread a run's graph checkpoints are kept under is derived from the run id too
(``run_thread_id``); the checkpoints themselves are not this module's
(``graph_checkpoints``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import Select, func, select, tuple_, update
from sqlalchemy.orm import Session

from openmarketer_core.agent_runtime.threads import thread_id
from openmarketer_core.db.models import (
    RUN_ERROR_MAX_LENGTH,
    Project,
    ResearchRunRecord,
    ResearchRunStatus,
)
from openmarketer_core.db.pages import CreatedPosition, Page, page_of, rows_to_fetch
from openmarketer_core.db.projects import get_project
from openmarketer_core.research.questions import (
    ResearchRequest,
    StoredResearchRequestInvalid,
)
from openmarketer_core.research.workflow_contract import RESEARCH_RUN_KIND

FINISHED = (ResearchRunStatus.SUCCEEDED, ResearchRunStatus.FAILED)

# What the name of every thread of a research run starts with. The checkpoint tables are
# shared by every graph, and a thread's name is all they know of it; the kind in front
# (``workflow_contract.RESEARCH_RUN_KIND``) says whose it is. A colon ends it because no run
# id holds one; the cleanup escapes the prefix anyway.
RESEARCH_RUN_THREADS = f"{RESEARCH_RUN_KIND}:"


def run_thread_id(run_id: uuid.UUID) -> str:
    """The thread a research run keeps its checkpoints under: its kind, then the run's id.

    ``db/checkpoint_cleanup.py`` finds the threads of research runs, and the run
    of each, by the same prefix.
    """
    return thread_id(RESEARCH_RUN_KIND, run_id)


class ResearchRunNotFound(Exception):
    """The workspace has no such research run of such a project."""

    def __init__(self, project_id: uuid.UUID, run_id: uuid.UUID) -> None:
        super().__init__(f"project {project_id} has no research run {run_id}")


class ResearchRunFinished(Exception):
    """The run succeeded or failed before; its outcome is recorded once."""

    def __init__(self, run_id: uuid.UUID, status: ResearchRunStatus) -> None:
        super().__init__(f"research run {run_id} has already {status.value}")


class ResearchRunNotStarted(Exception):
    """The run is still queued; only a run that was started can succeed."""

    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(f"research run {run_id} was never started")


@dataclass(frozen=True)
class ResearchRun:
    """One requested research run and how far it got."""

    id: uuid.UUID
    project_id: uuid.UUID
    status: ResearchRunStatus
    error: str | None
    report_id: uuid.UUID | None
    requested_by: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class ResearchRunToExecute:
    """The run ``run_to_execute`` found: its scope, its request and who asked."""

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    request: ResearchRequest
    requested_by: str


def request_research_run(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    request: ResearchRequest,
    requested_by: str,
) -> ResearchRun:
    """Add a queued research run for the project, with the request it answers.

    A project may have several research runs at once, so nothing refuses a
    second one. Raises ``ProjectNotFound`` when the workspace has no such
    project.
    """
    project = get_project(session, workspace_id=workspace_id, project_id=project_id)
    record = ResearchRunRecord(
        project_id=project.id,
        requested_by=requested_by,
        questions=list(request.questions),
        scope={
            "topics": list(request.topics),
            "competitors": list(request.competitors),
            "time_window": request.time_window,
        },
        max_sources=request.max_sources,
    )
    session.add(record)
    session.flush()
    return _research_run(record)


def mark_run_started(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> ResearchRun:
    """Record that a worker picked the run up. Safe to repeat while it is running."""
    record = _run_to_change(session, workspace_id, project_id, run_id)
    if record.status in FINISHED:
        raise ResearchRunFinished(run_id, record.status)
    if record.status is ResearchRunStatus.QUEUED:
        _change(session, record, status=ResearchRunStatus.RUNNING, started_at=func.now())
    return _research_run(record)


def mark_run_succeeded(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    report_id: uuid.UUID,
) -> ResearchRun:
    """Record that the run stored the report ``report_id`` and is finished.

    The report must belong to the run's project; the database refuses any other.
    """
    record = _run_to_change(session, workspace_id, project_id, run_id)
    if record.status is ResearchRunStatus.QUEUED:
        raise ResearchRunNotStarted(run_id)
    if record.status is ResearchRunStatus.RUNNING:
        _change(
            session,
            record,
            status=ResearchRunStatus.SUCCEEDED,
            report_id=report_id,
            finished_at=func.now(),
        )
    elif record.report_id != report_id:
        raise ResearchRunFinished(run_id, record.status)
    return _research_run(record)


def mark_run_failed(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    error: str,
) -> ResearchRun:
    """Record that the run ended without a report, keeping the start of ``error``.

    The message is shortened here rather than refused, so that a failure can
    always be recorded.
    """
    record = _run_to_change(session, workspace_id, project_id, run_id)
    if record.status is ResearchRunStatus.SUCCEEDED:
        raise ResearchRunFinished(run_id, record.status)
    if record.status is not ResearchRunStatus.FAILED:
        _change(
            session,
            record,
            status=ResearchRunStatus.FAILED,
            error=error[:RUN_ERROR_MAX_LENGTH],
            finished_at=func.now(),
        )
    return _research_run(record)


def research_run(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> ResearchRun:
    """The run as it is now."""
    record = session.scalar(_run_of_project(workspace_id, project_id, run_id))
    if record is None:
        raise ResearchRunNotFound(project_id, run_id)
    return _research_run(record)


def run_to_execute(session: Session, run_id: uuid.UUID) -> ResearchRunToExecute | None:
    """The run ``run_id`` with its project, workspace, request and asker, or ``None``.

    This one read is not scoped by a workspace, because it is what finds the
    scope: the worker's graph is handed a run by its id alone (the id this
    module made and the API gave the run's workflow, never a value a request or
    a model chose), and everything it then does with the run goes through the
    scoped functions with what this returns. It says nothing else of the run and
    changes nothing. A stored request that no longer fits its schema raises
    ``StoredResearchRequestInvalid``.
    """
    found = session.execute(
        select(Project.workspace_id, ResearchRunRecord)
        .join(Project, Project.id == ResearchRunRecord.project_id)
        .where(ResearchRunRecord.id == run_id)
    ).one_or_none()
    if found is None:
        return None
    workspace_id, record = found
    return ResearchRunToExecute(
        workspace_id=workspace_id,
        project_id=record.project_id,
        run_id=record.id,
        request=_stored_request(record, run_id),
        requested_by=record.requested_by,
    )


def list_research_runs(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    limit: int,
    before: CreatedPosition | None = None,
) -> Page[ResearchRun, CreatedPosition]:
    """The project's research runs as they are now, newest first.

    At most ``limit`` of them, starting after ``before`` (the ``next_before``
    of the previous page) or at the newest. Raises ``ProjectNotFound`` when the
    workspace has no such project.
    """
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    query = (
        select(ResearchRunRecord)
        .join(Project, Project.id == ResearchRunRecord.project_id)
        .where(Project.workspace_id == workspace_id, Project.id == project_id)
    )
    if before is not None:
        query = query.where(
            tuple_(ResearchRunRecord.created_at, ResearchRunRecord.id)
            < tuple_(before.created_at, before.id)
        )
    rows = session.scalars(
        query.order_by(ResearchRunRecord.created_at.desc(), ResearchRunRecord.id.desc()).limit(
            rows_to_fetch(limit)
        )
    )
    runs = [_research_run(record) for record in rows]
    return page_of(runs, limit, lambda last: CreatedPosition(last.created_at, last.id))


def _stored_request(record: ResearchRunRecord, run_id: uuid.UUID) -> ResearchRequest:
    """The request stored with the run, checked again against the schema."""
    scope = record.scope if isinstance(record.scope, dict) else {}
    try:
        return ResearchRequest(
            questions=record.questions,
            topics=scope.get("topics") or [],
            competitors=scope.get("competitors") or [],
            time_window=scope.get("time_window") or "",
            max_sources=record.max_sources,
        )
    except ValidationError as e:
        raise StoredResearchRequestInvalid(run_id) from e


def _run_of_project(
    workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> Select[ResearchRunRecord]:
    return (
        select(ResearchRunRecord)
        .join(Project, Project.id == ResearchRunRecord.project_id)
        .where(
            Project.workspace_id == workspace_id,
            Project.id == project_id,
            ResearchRunRecord.id == run_id,
        )
    )


def _run_to_change(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> ResearchRunRecord:
    """The run, with its row held until the transaction ends.

    Two reports about one run then apply one after the other, and the second
    sees what the first recorded.
    """
    record = session.scalar(
        _run_of_project(workspace_id, project_id, run_id)
        .with_for_update(of=ResearchRunRecord)
        # A row the session still holds from before it waited may have changed since.
        .execution_options(populate_existing=True)
    )
    if record is None:
        raise ResearchRunNotFound(project_id, run_id)
    return record


def _change(session: Session, record: ResearchRunRecord, **columns: object) -> None:
    session.execute(
        update(ResearchRunRecord).where(ResearchRunRecord.id == record.id).values(**columns)
    )
    session.refresh(record)


def _research_run(record: ResearchRunRecord) -> ResearchRun:
    return ResearchRun(
        id=record.id,
        project_id=record.project_id,
        status=record.status,
        error=record.error,
        report_id=record.report_id,
        requested_by=record.requested_by,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )
