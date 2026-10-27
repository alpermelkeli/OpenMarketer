"""Writing runs: the state of each requested post of a project.

The API requests a run and later asks for its status; the worker reports that it started,
which profile version it writes from, and how it ended. The row is the only shared record of
that, so a run's state survives a restart of either. Like research, a project may have any
number of writing runs at once.

A run moves forwards only:

- requested: ``queued``;
- ``queued`` to ``running`` when the worker picks it up. Reporting the start again changes
  nothing, because a retried activity reports it again. The start may carry the approved
  profile version the run's ``prepare`` chose; it is recorded once and never replaced
  (``WritingRunVersionChosen``), and it must be an approved version of the run's project
  (``ProfileVersionNotApproved``);
- ``running`` to ``succeeded`` once its content item (``db/content_items.py``) is stored and
  settled: approved and recorded as a dry run, rejected or expired. Reporting the same again
  changes nothing;
- ``queued`` or ``running`` to ``failed``, with a message. A content item it proposed and that
  was not settled is settled ``failed`` with it. Reporting a failure of a run that already
  failed changes nothing and keeps the first message.

Everything else is refused: a finished run is never started, failed or given another outcome
(``WritingRunFinished``), a run that was never started cannot succeed
(``WritingRunNotStarted``), and a run whose item is missing or still proposed cannot either
(``WritingRunUnsettled``). What the run's model calls cost is recorded with its outcome.

The brief is stored with the run and read back strictly whenever the run is executed
(``run_to_execute``) or shown (``requested_brief``): a stored brief that no longer fits its
schema is ``StoredWritingBriefInvalid``.

Every function but one is scoped to a workspace and a project, including those the worker
calls: a run of another workspace or project is treated exactly like a run that does not
exist. The one is ``run_to_execute``, which is how the worker learns the scope of a run it was
handed by its id. Functions take a session and never commit; a transition holds the run's row.

This module does not start or cancel workflows, and stores no workflow id: the caller derives
it from the run id (``writing/workflow_contract.py``). The thread a run's graph checkpoints are
kept under is derived from the run id too (``run_thread_id``). Runs are deleted with their
project (``db/project_removal.py``); deleting one run on its own is not built yet.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import Select, func, select, tuple_, update
from sqlalchemy.orm import Session

from openmarketer_core.agent_runtime.threads import thread_id
from openmarketer_core.db.chat import require_chat
from openmarketer_core.db.models import (
    RUN_ERROR_MAX_LENGTH,
    ContentItemRecord,
    ContentItemStatus,
    ProductProfileRecord,
    ProfileStatus,
    Project,
    WritingRunRecord,
    WritingRunStatus,
)
from openmarketer_core.db.pages import CreatedPosition, Page, page_of, rows_to_fetch
from openmarketer_core.db.projects import get_project
from openmarketer_core.writing.brief import (
    StoredWritingBriefInvalid,
    WritingBrief,
    WritingPlatform,
)
from openmarketer_core.writing.workflow_contract import WRITING_RUN_KIND

FINISHED = (WritingRunStatus.SUCCEEDED, WritingRunStatus.FAILED)

# What the name of every thread of a writing run starts with; see ``db/research_runs.py`` for
# why a kind and a colon.
WRITING_RUN_THREADS = f"{WRITING_RUN_KIND}:"


def run_thread_id(run_id: uuid.UUID) -> str:
    """The thread a writing run keeps its checkpoints under: its kind, then the run's id.

    ``db/checkpoint_cleanup.py`` finds the threads of writing runs, and the run of each, by
    the same prefix.
    """
    return thread_id(WRITING_RUN_KIND, run_id)


class WritingRunNotFound(Exception):
    """The workspace has no such writing run of such a project."""

    def __init__(self, project_id: uuid.UUID, run_id: uuid.UUID) -> None:
        super().__init__(f"project {project_id} has no writing run {run_id}")


class WritingRunFinished(Exception):
    """The run ended before; its outcome is recorded once."""

    def __init__(self, run_id: uuid.UUID, status: WritingRunStatus) -> None:
        self.status = status
        super().__init__(f"writing run {run_id} has already ended: {status.value}")


class WritingRunNotStarted(Exception):
    """The run is still queued; what is asked needs a run that was started."""

    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(f"writing run {run_id} was never started")


class WritingRunUnsettled(Exception):
    """The run has no content item, or its item still waits for a decision."""

    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(f"writing run {run_id} has no settled content item")


class WritingRunVersionChosen(Exception):
    """The run writes from another profile version, chosen before; it is chosen once."""

    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(f"writing run {run_id} already writes from another profile version")


class ProfileVersionNotApproved(Exception):
    """The version is not an approved profile version of the run's project."""

    def __init__(self, project_id: uuid.UUID, version_id: uuid.UUID) -> None:
        super().__init__(f"project {project_id} has no approved profile version {version_id}")


@dataclass(frozen=True)
class WritingRun:
    """One requested writing run and how far it got.

    ``profile_version_id`` is the approved version the run writes from, once chosen;
    ``content_item_id`` the post it proposed, once stored; ``chat_thread_id`` the chat whose
    turn asked for the run, which hears back when it ends, ``None`` for a run asked for
    anywhere else.
    """

    id: uuid.UUID
    project_id: uuid.UUID
    status: WritingRunStatus
    platform: WritingPlatform
    error: str | None
    profile_version_id: uuid.UUID | None
    content_item_id: uuid.UUID | None
    cost_usd: Decimal
    requested_by: str
    chat_thread_id: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class WritingRunToExecute:
    """The run ``run_to_execute`` found: its scope, its brief and who asked."""

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    brief: WritingBrief
    requested_by: str


def request_writing_run(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    brief: WritingBrief,
    requested_by: str,
    chat_thread_id: uuid.UUID | None = None,
) -> WritingRun:
    """Add a queued writing run for the project, with the brief it answers.

    ``chat_thread_id`` is the chat whose turn asked for the run, a chat of the same project.
    Raises ``ProjectNotFound`` when the workspace has no such project, and ``ChatNotFound``
    when the project has no such chat.
    """
    project = get_project(session, workspace_id=workspace_id, project_id=project_id)
    if chat_thread_id is not None:
        require_chat(
            session, workspace_id=workspace_id, project_id=project_id, chat_id=chat_thread_id
        )
    record = WritingRunRecord(
        project_id=project.id,
        requested_by=requested_by,
        chat_thread_id=chat_thread_id,
        brief=brief.model_dump(mode="json"),
        platform=brief.platform.value,
    )
    session.add(record)
    session.flush()
    session.refresh(record)
    return _writing_run(record)


def mark_run_started(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    profile_version_id: uuid.UUID | None = None,
) -> WritingRun:
    """Record that a worker picked the run up, and the profile version it writes from.

    Safe to repeat while it is running. ``profile_version_id``, when given, must be an
    approved version of the run's project (``ProfileVersionNotApproved``), and is recorded
    once: the same version again changes nothing, another is ``WritingRunVersionChosen``.
    """
    record = _run_to_change(session, workspace_id, project_id, run_id)
    if record.status in FINISHED:
        raise WritingRunFinished(run_id, record.status)
    if profile_version_id is not None and record.profile_version_id != profile_version_id:
        if record.profile_version_id is not None:
            raise WritingRunVersionChosen(run_id)
        _require_approved_version(session, project_id, profile_version_id)
    columns: dict[str, object] = {}
    if record.status is WritingRunStatus.QUEUED:
        columns.update(status=WritingRunStatus.RUNNING, started_at=func.now())
    if profile_version_id is not None and record.profile_version_id is None:
        columns["profile_version_id"] = profile_version_id
    if columns:
        _change(session, record, **columns)
    return _writing_run(record)


def mark_run_succeeded(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    cost_usd: Decimal,
) -> WritingRun:
    """Record that the run's post is settled and the run is finished, with what it cost.

    Raises ``WritingRunNotStarted`` for a queued run, ``WritingRunUnsettled`` while its item
    is missing or still proposed, and ``WritingRunFinished`` for a run that failed.
    """
    record = _run_to_change(session, workspace_id, project_id, run_id)
    if record.status is WritingRunStatus.QUEUED:
        raise WritingRunNotStarted(run_id)
    if record.status is WritingRunStatus.SUCCEEDED:
        return _writing_run(record)
    if record.status is WritingRunStatus.FAILED:
        raise WritingRunFinished(run_id, record.status)
    item_status = session.scalar(
        select(ContentItemRecord.status).where(
            ContentItemRecord.id == record.content_item_id,
            ContentItemRecord.project_id == project_id,
        )
    )
    if item_status is None or item_status is ContentItemStatus.PROPOSED:
        raise WritingRunUnsettled(run_id)
    _change(
        session,
        record,
        status=WritingRunStatus.SUCCEEDED,
        cost_usd=cost_usd,
        finished_at=func.now(),
    )
    return _writing_run(record)


def mark_run_failed(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    error: str,
    cost_usd: Decimal = Decimal(0),
) -> WritingRun:
    """Record that the run ended without a settled post, keeping the start of ``error``.

    ``cost_usd`` is what it spent before it failed. An item it proposed that was not settled
    is settled ``failed``, in the same transaction. The message is shortened here rather than
    refused, so that a failure can always be recorded.
    """
    record = _run_to_change(session, workspace_id, project_id, run_id)
    if record.status is WritingRunStatus.SUCCEEDED:
        raise WritingRunFinished(run_id, record.status)
    if record.status is WritingRunStatus.FAILED:
        return _writing_run(record)
    if record.content_item_id is not None:
        session.execute(
            update(ContentItemRecord)
            .where(
                ContentItemRecord.id == record.content_item_id,
                ContentItemRecord.project_id == project_id,
                ContentItemRecord.status == ContentItemStatus.PROPOSED,
            )
            .values(status=ContentItemStatus.FAILED, settled_at=func.now())
        )
    _change(
        session,
        record,
        status=WritingRunStatus.FAILED,
        error=error[:RUN_ERROR_MAX_LENGTH],
        cost_usd=cost_usd,
        finished_at=func.now(),
    )
    return _writing_run(record)


def writing_run(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> WritingRun:
    """The run as it is now."""
    return _writing_run(_found_run(session, workspace_id, project_id, run_id))


def requested_brief(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> WritingBrief:
    """What the run was asked for, read back strictly.

    Raises ``WritingRunNotFound`` and ``StoredWritingBriefInvalid``.
    """
    return _stored_brief(_found_run(session, workspace_id, project_id, run_id))


def run_to_execute(session: Session, run_id: uuid.UUID) -> WritingRunToExecute | None:
    """The run ``run_id`` with its project, workspace, brief and asker, or ``None``.

    This one read is not scoped by a workspace, because it is what finds the scope: the
    worker's graph is handed a run by its id alone (the id this module made and the API gave
    the run's workflow, never a value a request or a model chose), and everything it then does
    with the run goes through the scoped functions with what this returns. It says nothing
    else of the run and changes nothing. A stored brief that no longer fits its schema raises
    ``StoredWritingBriefInvalid``.
    """
    found = session.execute(
        select(Project.workspace_id, WritingRunRecord)
        .join(Project, Project.id == WritingRunRecord.project_id)
        .where(WritingRunRecord.id == run_id)
    ).one_or_none()
    if found is None:
        return None
    workspace_id, record = found
    return WritingRunToExecute(
        workspace_id=workspace_id,
        project_id=record.project_id,
        run_id=record.id,
        brief=_stored_brief(record),
        requested_by=record.requested_by,
    )


def list_writing_runs(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    limit: int,
    before: CreatedPosition | None = None,
) -> Page[WritingRun, CreatedPosition]:
    """The project's writing runs as they are now, newest first.

    At most ``limit`` of them, starting after ``before`` (the ``next_before`` of the previous
    page) or at the newest. Raises ``ProjectNotFound`` when the workspace has no such project.
    """
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    query = (
        select(WritingRunRecord)
        .join(Project, Project.id == WritingRunRecord.project_id)
        .where(Project.workspace_id == workspace_id, Project.id == project_id)
    )
    if before is not None:
        query = query.where(
            tuple_(WritingRunRecord.created_at, WritingRunRecord.id)
            < tuple_(before.created_at, before.id)
        )
    rows = session.scalars(
        query.order_by(WritingRunRecord.created_at.desc(), WritingRunRecord.id.desc()).limit(
            rows_to_fetch(limit)
        )
    )
    runs = [_writing_run(record) for record in rows]
    return page_of(runs, limit, lambda last: CreatedPosition(last.created_at, last.id))


def _require_approved_version(
    session: Session, project_id: uuid.UUID, version_id: uuid.UUID
) -> None:
    found = session.scalar(
        select(ProductProfileRecord.id).where(
            ProductProfileRecord.id == version_id,
            ProductProfileRecord.project_id == project_id,
            ProductProfileRecord.status == ProfileStatus.APPROVED,
        )
    )
    if found is None:
        raise ProfileVersionNotApproved(project_id, version_id)


def _stored_brief(record: WritingRunRecord) -> WritingBrief:
    """The brief stored with the run, checked again against the schema."""
    try:
        return WritingBrief.model_validate(record.brief)
    except ValidationError as e:
        raise StoredWritingBriefInvalid(record.id) from e


def _run_of_project(
    workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> Select[WritingRunRecord]:
    return (
        select(WritingRunRecord)
        .join(Project, Project.id == WritingRunRecord.project_id)
        .where(
            Project.workspace_id == workspace_id,
            Project.id == project_id,
            WritingRunRecord.id == run_id,
        )
    )


def _found_run(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> WritingRunRecord:
    record = session.scalar(_run_of_project(workspace_id, project_id, run_id))
    if record is None:
        raise WritingRunNotFound(project_id, run_id)
    return record


def _run_to_change(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> WritingRunRecord:
    """The run, with its row held until the transaction ends.

    Two reports about one run then apply one after the other, and the second sees what the
    first recorded.
    """
    record = session.scalar(
        _run_of_project(workspace_id, project_id, run_id)
        .with_for_update(of=WritingRunRecord)
        # A row the session still holds from before it waited may have changed since.
        .execution_options(populate_existing=True)
    )
    if record is None:
        raise WritingRunNotFound(project_id, run_id)
    return record


def _change(session: Session, record: WritingRunRecord, **columns: object) -> None:
    session.execute(
        update(WritingRunRecord).where(WritingRunRecord.id == record.id).values(**columns)
    )
    session.refresh(record)


def _writing_run(record: WritingRunRecord) -> WritingRun:
    return WritingRun(
        id=record.id,
        project_id=record.project_id,
        status=record.status,
        platform=WritingPlatform(record.platform),
        error=record.error,
        profile_version_id=record.profile_version_id,
        content_item_id=record.content_item_id,
        cost_usd=record.cost_usd,
        requested_by=record.requested_by,
        chat_thread_id=record.chat_thread_id,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )
