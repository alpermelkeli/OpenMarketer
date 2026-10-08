"""Analysis runs: the state of each requested analysis of a project.

The API requests a run and later asks for its status; the worker reports that
it started and how it ended. The row is the only shared record of that, so a
run's state survives a restart of either. A project has at most one unfinished
run at a time, which keeps a repeated request from paying for the model twice.

A run moves forwards only:

- requested: ``queued``;
- ``queued`` to ``running`` when the worker picks it up. Reporting the start of
  a run that is already running changes nothing, because a retried activity
  reports it again;
- ``running`` to ``succeeded``, with the profile version the run stored.
  Reporting the same success again changes nothing;
- ``queued`` or ``running`` to ``failed``, with a message. A run can fail
  before it starts, for instance when its workflow cannot be started.
  Reporting a failure of a run that already failed changes nothing and keeps
  the first message.

Everything else is refused: a finished run is never started, failed or given
another result (``AnalysisRunFinished``), and a run that was never started
cannot succeed (``AnalysisRunNotStarted``).

Every function is scoped to a workspace and a project, including those the
worker calls: a run of another workspace or project is treated exactly like a
run that does not exist. Functions take a session and never commit, so the
worker can store a run's result (``evidence_store.save_analysis_of_project``)
and report its success in one transaction.

This module does not start or cancel workflows, and stores no workflow id: the
caller derives it from the run id. It does not time out a run whose worker
died, does not delete runs, and does not redact the failure message, which it
only shortens.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openmarketer_core.db.evidence_store import project_in_workspace
from openmarketer_core.db.models import (
    ANALYSIS_ERROR_MAX_LENGTH,
    AnalysisRunRecord,
    AnalysisRunStatus,
    ProductProfileRecord,
    Project,
)

UNFINISHED_RUN_INDEX = "uq_analysis_run_project_id_unfinished"
FINISHED = (AnalysisRunStatus.SUCCEEDED, AnalysisRunStatus.FAILED)


class AnalysisRunNotFound(Exception):
    """The workspace has no such run of such a project."""

    def __init__(self, project_id: uuid.UUID, run_id: uuid.UUID) -> None:
        super().__init__(f"project {project_id} has no analysis run {run_id}")


class AnalysisAlreadyRunning(Exception):
    """The project has a run that is queued or running; it gets no second one."""

    def __init__(self, project_id: uuid.UUID) -> None:
        super().__init__(f"project {project_id} already has an unfinished analysis run")


class AnalysisRunFinished(Exception):
    """The run succeeded or failed before; its outcome is recorded once."""

    def __init__(self, run_id: uuid.UUID, status: AnalysisRunStatus) -> None:
        super().__init__(f"analysis run {run_id} has already {status.value}")


class AnalysisRunNotStarted(Exception):
    """The run is still queued; only a run that was started can succeed."""

    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(f"analysis run {run_id} was never started")


@dataclass(frozen=True)
class AnalysisRun:
    """One requested analysis of a project and how far it got."""

    id: uuid.UUID
    project_id: uuid.UUID
    status: AnalysisRunStatus
    error: str | None
    profile_id: uuid.UUID | None
    profile_version: int | None
    snapshot_id: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def request_analysis_run(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> AnalysisRun:
    """Add a queued run for the project, unless it has an unfinished one."""
    project = project_in_workspace(session, workspace_id, project_id)
    record = AnalysisRunRecord(project_id=project.id)
    try:
        # A savepoint, so that a refusal leaves the caller's transaction usable.
        with session.begin_nested():
            session.add(record)
            session.flush()
    except IntegrityError as e:
        if UNFINISHED_RUN_INDEX not in str(e.orig):
            raise
        raise AnalysisAlreadyRunning(project_id) from e
    return _analysis_run(session, record)


def mark_run_started(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> AnalysisRun:
    """Record that a worker picked the run up. Safe to repeat while it is running."""
    record = _run_to_change(session, workspace_id, project_id, run_id)
    if record.status in FINISHED:
        raise AnalysisRunFinished(run_id, record.status)
    if record.status is AnalysisRunStatus.QUEUED:
        _change(session, record, status=AnalysisRunStatus.RUNNING, started_at=func.now())
    return _analysis_run(session, record)


def mark_run_succeeded(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    profile_id: uuid.UUID,
) -> AnalysisRun:
    """Record that the run stored the profile version ``profile_id`` and is finished.

    The version must belong to the run's project; the database refuses any other.
    """
    record = _run_to_change(session, workspace_id, project_id, run_id)
    if record.status is AnalysisRunStatus.QUEUED:
        raise AnalysisRunNotStarted(run_id)
    if record.status is AnalysisRunStatus.RUNNING:
        _change(
            session,
            record,
            status=AnalysisRunStatus.SUCCEEDED,
            profile_id=profile_id,
            finished_at=func.now(),
        )
    elif record.profile_id != profile_id:
        raise AnalysisRunFinished(run_id, record.status)
    return _analysis_run(session, record)


def mark_run_failed(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    error: str,
) -> AnalysisRun:
    """Record that the run ended without a result, keeping the start of ``error``.

    The message is shortened here rather than refused, so that a failure can
    always be recorded.
    """
    record = _run_to_change(session, workspace_id, project_id, run_id)
    if record.status is AnalysisRunStatus.SUCCEEDED:
        raise AnalysisRunFinished(run_id, record.status)
    if record.status is not AnalysisRunStatus.FAILED:
        _change(
            session,
            record,
            status=AnalysisRunStatus.FAILED,
            error=error[:ANALYSIS_ERROR_MAX_LENGTH],
            finished_at=func.now(),
        )
    return _analysis_run(session, record)


def analysis_run(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> AnalysisRun:
    """The run as it is now."""
    record = session.scalar(_run_of_project(workspace_id, project_id, run_id))
    if record is None:
        raise AnalysisRunNotFound(project_id, run_id)
    return _analysis_run(session, record)


def _run_of_project(
    workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> Select[AnalysisRunRecord]:
    return (
        select(AnalysisRunRecord)
        .join(Project, Project.id == AnalysisRunRecord.project_id)
        .where(
            Project.workspace_id == workspace_id,
            Project.id == project_id,
            AnalysisRunRecord.id == run_id,
        )
    )


def _run_to_change(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> AnalysisRunRecord:
    """The run, with its row held until the transaction ends.

    Two reports about one run then apply one after the other, and the second
    sees what the first recorded.
    """
    record = session.scalar(
        _run_of_project(workspace_id, project_id, run_id)
        .with_for_update(of=AnalysisRunRecord)
        # A row the session still holds from before it waited may have changed since.
        .execution_options(populate_existing=True)
    )
    if record is None:
        raise AnalysisRunNotFound(project_id, run_id)
    return record


def _change(session: Session, record: AnalysisRunRecord, **columns: object) -> None:
    session.execute(
        update(AnalysisRunRecord).where(AnalysisRunRecord.id == record.id).values(**columns)
    )
    session.refresh(record)


def _analysis_run(session: Session, record: AnalysisRunRecord) -> AnalysisRun:
    profile = (
        None
        if record.profile_id is None
        else session.get_one(ProductProfileRecord, record.profile_id)
    )
    return AnalysisRun(
        id=record.id,
        project_id=record.project_id,
        status=record.status,
        error=record.error,
        profile_id=record.profile_id,
        profile_version=None if profile is None else profile.version,
        snapshot_id=None if profile is None else profile.snapshot_id,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )
