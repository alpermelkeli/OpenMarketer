"""Studio runs: the state of each requested run of the creative studio of a project.

The API requests a run and later asks for its status; the worker reports that it started and
how it ended. The row is the only shared record of that, so a run's state survives a restart
of either. Like research, a project may have any number of studio runs at once.

A run moves forwards only:

- requested: ``queued``;
- ``queued`` to ``running`` when the worker picks it up. Reporting the start of a run that is
  already running changes nothing, because a retried activity reports it again;
- ``running`` to ``succeeded``, with the version that passed, or to ``needs_review``, with the
  best candidate, when a limit was hit and a person decides. Either names the version's asset
  as the run's final asset and makes the version that asset's current one; reporting the same
  outcome with the same version again changes nothing;
- ``queued`` or ``running`` to ``failed``, with a message. Reporting a failure of a run that
  already failed changes nothing and keeps the first message.

Everything else is refused: a finished run is never started, failed or given another outcome
(``StudioRunFinished``), and a run that was never started cannot end with an asset
(``StudioRunNotStarted``). What the run spent is recorded with its outcome.

A finished run can be deleted with its assets and their versions (``delete_studio_run``); a
run that is queued or running cannot (``StudioRunUnfinished``), because the worker would store
images for a run that is gone.

The request is stored with the run and read back strictly whenever the run is executed
(``run_to_execute``) or shown (``requested_asset``): a stored request that no longer fits its
schema is ``StoredAssetRequestInvalid``. The assets a request names as references must be the
project's when the run is requested (``ReferenceAssetNotFound``).

Every function but one is scoped to a workspace and a project, including those the worker
calls: a run of another workspace or project is treated exactly like a run that does not
exist. The one is ``run_to_execute``, which is how the worker learns the scope of a run it was
handed by its id. Functions take a session and never commit; a transition holds the run's row.

This module does not start or cancel workflows, and stores no workflow id: the caller derives
it from the run id (``studio/workflow_contract.py``). The thread a run's graph checkpoints are
kept under is derived from the run id too (``run_thread_id``). The assets and versions
themselves are written in ``db/media_assets.py``; their images are not this module's
(``studio/asset_store.py``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import Select, delete, func, select, tuple_, update
from sqlalchemy.orm import Session

from openmarketer_core.agent_runtime.threads import thread_id
from openmarketer_core.db.chat import require_chat
from openmarketer_core.db.models import (
    RUN_ERROR_MAX_LENGTH,
    MediaAssetRecord,
    MediaAssetVersionRecord,
    Project,
    StudioRunRecord,
    StudioRunStatus,
)
from openmarketer_core.db.pages import CreatedPosition, Page, page_of, rows_to_fetch
from openmarketer_core.db.projects import get_project
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.requests import AssetRequest, StoredAssetRequestInvalid
from openmarketer_core.studio.workflow_contract import STUDIO_RUN_KIND

FINISHED = (StudioRunStatus.SUCCEEDED, StudioRunStatus.NEEDS_REVIEW, StudioRunStatus.FAILED)
# The outcomes that end a run with an asset.
WITH_ASSET = (StudioRunStatus.SUCCEEDED, StudioRunStatus.NEEDS_REVIEW)

# What the name of every thread of a studio run starts with; see ``db/research_runs.py`` for
# why a kind and a colon.
STUDIO_RUN_THREADS = f"{STUDIO_RUN_KIND}:"


def run_thread_id(run_id: uuid.UUID) -> str:
    """The thread a studio run keeps its checkpoints under: its kind, then the run's id.

    ``db/checkpoint_cleanup.py`` finds the threads of studio runs, and the run of each, by
    the same prefix.
    """
    return thread_id(STUDIO_RUN_KIND, run_id)


class StudioRunNotFound(Exception):
    """The workspace has no such studio run of such a project."""

    def __init__(self, project_id: uuid.UUID, run_id: uuid.UUID) -> None:
        super().__init__(f"project {project_id} has no studio run {run_id}")


class StudioRunFinished(Exception):
    """The run ended before; its outcome is recorded once."""

    def __init__(self, run_id: uuid.UUID, status: StudioRunStatus) -> None:
        self.status = status
        super().__init__(f"studio run {run_id} has already ended: {status.value}")


class StudioRunNotStarted(Exception):
    """The run is still queued; only a run that was started can end with an asset."""

    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(f"studio run {run_id} was never started")


class StudioRunUnfinished(Exception):
    """The run is queued or running; what is asked of it waits until it has ended."""

    def __init__(self, run_id: uuid.UUID, status: StudioRunStatus) -> None:
        self.status = status
        super().__init__(f"studio run {run_id} is still {status.value}")


class ReferenceAssetNotFound(Exception):
    """A request names a reference asset the project does not have."""

    def __init__(self, project_id: uuid.UUID, asset_id: uuid.UUID) -> None:
        super().__init__(f"project {project_id} has no media asset {asset_id} to refer to")


class FinalVersionNotFound(Exception):
    """The version a run is to end with is not a version of one of the run's assets."""

    def __init__(self, run_id: uuid.UUID, version_id: uuid.UUID) -> None:
        super().__init__(f"studio run {run_id} has no asset version {version_id}")


@dataclass(frozen=True)
class StudioRun:
    """One requested studio run and how far it got.

    ``final_asset_id`` is the asset a run that succeeded or needs review ended with;
    ``chat_thread_id`` is the chat whose turn asked for the run, which hears back when it
    ends; ``None`` for a run asked for anywhere else.
    """

    id: uuid.UUID
    project_id: uuid.UUID
    status: StudioRunStatus
    platform: Platform
    error: str | None
    final_asset_id: uuid.UUID | None
    cost_usd: Decimal
    requested_by: str
    chat_thread_id: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class StudioRunToExecute:
    """The run ``run_to_execute`` found: its scope, its request and who asked."""

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    request: AssetRequest
    requested_by: str


def request_studio_run(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    request: AssetRequest,
    requested_by: str,
    origin_action_id: str | None = None,
    chat_thread_id: uuid.UUID | None = None,
) -> StudioRun:
    """Add a queued studio run for the project, with the request it answers.

    With ``origin_action_id`` -- the idempotency key of the action of another run that asked
    for this one -- a run already asked for under that key is found and given back instead.
    ``chat_thread_id`` is the chat whose turn asked for the run, a chat of the same project.
    Raises ``ProjectNotFound`` when the workspace has no such project, ``ChatNotFound`` when
    the project has no such chat, and ``ReferenceAssetNotFound`` when a reference asset is not
    one of the project's.
    """
    project = get_project(session, workspace_id=workspace_id, project_id=project_id)
    if chat_thread_id is not None:
        require_chat(
            session, workspace_id=workspace_id, project_id=project_id, chat_id=chat_thread_id
        )
    if origin_action_id is not None:
        asked_before = session.scalar(
            select(StudioRunRecord).where(
                StudioRunRecord.project_id == project.id,
                StudioRunRecord.origin_action_id == origin_action_id,
            )
        )
        if asked_before is not None:
            return _studio_run(asked_before)
    _require_assets_of_project(session, project.id, request.reference_asset_ids)
    record = StudioRunRecord(
        project_id=project.id,
        requested_by=requested_by,
        origin_action_id=origin_action_id,
        chat_thread_id=chat_thread_id,
        request=request.model_dump(mode="json"),
        platform=request.platform.value,
    )
    session.add(record)
    session.flush()
    session.refresh(record)
    return _studio_run(record)


def mark_run_started(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> StudioRun:
    """Record that a worker picked the run up. Safe to repeat while it is running."""
    record = _run_to_change(session, workspace_id, project_id, run_id)
    if record.status in FINISHED:
        raise StudioRunFinished(run_id, record.status)
    if record.status is StudioRunStatus.QUEUED:
        _change(session, record, status=StudioRunStatus.RUNNING, started_at=func.now())
    return _studio_run(record)


def mark_run_succeeded(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    final_version_id: uuid.UUID,
    cost_usd: Decimal,
) -> StudioRun:
    """Record that the version ``final_version_id`` passed and the run is finished.

    The version's asset becomes the run's final asset, and the version its current one.
    Raises ``FinalVersionNotFound`` when the version is not one of the run's.
    """
    return _end_with_asset(
        session,
        workspace_id,
        project_id,
        run_id,
        StudioRunStatus.SUCCEEDED,
        final_version_id,
        cost_usd,
    )


def mark_run_needs_review(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    final_version_id: uuid.UUID,
    cost_usd: Decimal,
) -> StudioRun:
    """Record that the run hit a limit and its best candidate, ``final_version_id``, waits.

    As for a success, the version's asset becomes the run's final asset and the version its
    current one; a person then accepts or rejects the asset. Raises ``FinalVersionNotFound``
    when the version is not one of the run's.
    """
    return _end_with_asset(
        session,
        workspace_id,
        project_id,
        run_id,
        StudioRunStatus.NEEDS_REVIEW,
        final_version_id,
        cost_usd,
    )


def mark_run_failed(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    error: str,
    cost_usd: Decimal = Decimal(0),
) -> StudioRun:
    """Record that the run ended without an asset, keeping the start of ``error``.

    ``cost_usd`` is what it spent before it failed. The message is shortened here rather
    than refused, so that a failure can always be recorded.
    """
    record = _run_to_change(session, workspace_id, project_id, run_id)
    if record.status in WITH_ASSET:
        raise StudioRunFinished(run_id, record.status)
    if record.status is not StudioRunStatus.FAILED:
        _change(
            session,
            record,
            status=StudioRunStatus.FAILED,
            error=error[:RUN_ERROR_MAX_LENGTH],
            cost_usd=cost_usd,
            finished_at=func.now(),
        )
    return _studio_run(record)


def studio_run(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> StudioRun:
    """The run as it is now."""
    return _studio_run(_found_run(session, workspace_id, project_id, run_id))


def requested_asset(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> AssetRequest:
    """What the run was asked for, read back strictly.

    Raises ``StudioRunNotFound`` and ``StoredAssetRequestInvalid``.
    """
    return _stored_request(_found_run(session, workspace_id, project_id, run_id))


def run_to_execute(session: Session, run_id: uuid.UUID) -> StudioRunToExecute | None:
    """The run ``run_id`` with its project, workspace, request and asker, or ``None``.

    This one read is not scoped by a workspace, because it is what finds the scope: the
    worker's graph is handed a run by its id alone (the id this module made and the API gave
    the run's workflow, never a value a request or a model chose), and everything it then does
    with the run goes through the scoped functions with what this returns. It says nothing
    else of the run and changes nothing. A stored request that no longer fits its schema
    raises ``StoredAssetRequestInvalid``.
    """
    found = session.execute(
        select(Project.workspace_id, StudioRunRecord)
        .join(Project, Project.id == StudioRunRecord.project_id)
        .where(StudioRunRecord.id == run_id)
    ).one_or_none()
    if found is None:
        return None
    workspace_id, record = found
    return StudioRunToExecute(
        workspace_id=workspace_id,
        project_id=record.project_id,
        run_id=record.id,
        request=_stored_request(record),
        requested_by=record.requested_by,
    )


def list_studio_runs(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    limit: int,
    before: CreatedPosition | None = None,
) -> Page[StudioRun, CreatedPosition]:
    """The project's studio runs as they are now, newest first.

    At most ``limit`` of them, starting after ``before`` (the ``next_before`` of the previous
    page) or at the newest. Raises ``ProjectNotFound`` when the workspace has no such project.
    """
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    query = (
        select(StudioRunRecord)
        .join(Project, Project.id == StudioRunRecord.project_id)
        .where(Project.workspace_id == workspace_id, Project.id == project_id)
    )
    if before is not None:
        query = query.where(
            tuple_(StudioRunRecord.created_at, StudioRunRecord.id)
            < tuple_(before.created_at, before.id)
        )
    rows = session.scalars(
        query.order_by(StudioRunRecord.created_at.desc(), StudioRunRecord.id.desc()).limit(
            rows_to_fetch(limit)
        )
    )
    runs = [_studio_run(record) for record in rows]
    return page_of(runs, limit, lambda last: CreatedPosition(last.created_at, last.id))


def delete_studio_run(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> tuple[str, ...]:
    """Delete a finished run with its assets and their versions, for good.

    Nothing is kept to restore it from, an accepted asset included. Answers the storage keys
    of the removed versions' images; the caller removes each key's object from object storage
    before its transaction commits. Raises ``StudioRunNotFound`` when the workspace has no
    such run of the project, and ``StudioRunUnfinished`` while the run is queued or running:
    the worker would store images for a run that is gone, and nothing here stops its workflow.
    """
    record = _run_to_change(session, workspace_id, project_id, run_id)
    if record.status not in FINISHED:
        raise StudioRunUnfinished(run_id, record.status)
    assets = select(MediaAssetRecord.id).where(
        MediaAssetRecord.project_id == project_id, MediaAssetRecord.run_id == run_id
    )
    keys = tuple(
        session.scalars(
            select(MediaAssetVersionRecord.storage_key).where(
                MediaAssetVersionRecord.project_id == project_id,
                MediaAssetVersionRecord.asset_id.in_(assets),
            )
        )
    )
    remove_studio_rows(session, project_id=project_id, run_id=run_id)
    return keys


def remove_studio_rows(
    session: Session, *, project_id: uuid.UUID, run_id: uuid.UUID | None = None
) -> None:
    """Delete studio runs with their assets and versions: one run, or all of the project's.

    The run, its assets and their versions name each other (a run its final asset, an asset
    its run and its current version, a version its asset), so no order of separate deletes
    satisfies every foreign key. One statement deletes all three, and the keys are checked
    when it ends, with nothing left that names what went. The caller has found the run or the
    project in its workspace and decided it may go; the images are the caller's.
    """
    runs = select(StudioRunRecord.id).where(StudioRunRecord.project_id == project_id)
    if run_id is not None:
        runs = runs.where(StudioRunRecord.id == run_id)
    assets = select(MediaAssetRecord.id).where(
        MediaAssetRecord.project_id == project_id, MediaAssetRecord.run_id.in_(runs)
    )
    removed_versions = (
        delete(MediaAssetVersionRecord)
        .where(
            MediaAssetVersionRecord.project_id == project_id,
            MediaAssetVersionRecord.asset_id.in_(assets),
        )
        .returning(MediaAssetVersionRecord.id)
        .cte("removed_versions")
    )
    removed_assets = (
        delete(MediaAssetRecord)
        .where(MediaAssetRecord.project_id == project_id, MediaAssetRecord.run_id.in_(runs))
        .returning(MediaAssetRecord.id)
        .cte("removed_assets")
    )
    session.execute(
        delete(StudioRunRecord)
        .where(StudioRunRecord.project_id == project_id, StudioRunRecord.id.in_(runs))
        .add_cte(removed_versions)
        .add_cte(removed_assets)
    )


def _end_with_asset(
    session: Session,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    status: StudioRunStatus,
    final_version_id: uuid.UUID,
    cost_usd: Decimal,
) -> StudioRun:
    record = _run_to_change(session, workspace_id, project_id, run_id)
    if record.status is StudioRunStatus.QUEUED:
        raise StudioRunNotStarted(run_id)
    if record.status is not StudioRunStatus.RUNNING:
        if record.status is not status or not _ends_with(session, record, final_version_id):
            raise StudioRunFinished(run_id, record.status)
        return _studio_run(record)
    asset_id = session.scalar(
        select(MediaAssetVersionRecord.asset_id)
        .join(MediaAssetRecord, MediaAssetRecord.id == MediaAssetVersionRecord.asset_id)
        .where(
            MediaAssetVersionRecord.id == final_version_id,
            MediaAssetRecord.project_id == project_id,
            MediaAssetRecord.run_id == run_id,
        )
    )
    if asset_id is None:
        raise FinalVersionNotFound(run_id, final_version_id)
    session.execute(
        update(MediaAssetRecord)
        .where(MediaAssetRecord.id == asset_id)
        .values(current_version_id=final_version_id)
    )
    _change(
        session,
        record,
        status=status,
        final_asset_id=asset_id,
        cost_usd=cost_usd,
        finished_at=func.now(),
    )
    return _studio_run(record)


def _ends_with(session: Session, record: StudioRunRecord, version_id: uuid.UUID) -> bool:
    """Whether the run's final asset stands for ``version_id``: the outcome is repeated."""
    current = session.scalar(
        select(MediaAssetRecord.current_version_id).where(
            MediaAssetRecord.id == record.final_asset_id
        )
    )
    return current == version_id


def _require_assets_of_project(
    session: Session, project_id: uuid.UUID, asset_ids: list[uuid.UUID]
) -> None:
    if not asset_ids:
        return
    found = set(
        session.scalars(
            select(MediaAssetRecord.id).where(
                MediaAssetRecord.project_id == project_id, MediaAssetRecord.id.in_(asset_ids)
            )
        )
    )
    for asset_id in asset_ids:
        if asset_id not in found:
            raise ReferenceAssetNotFound(project_id, asset_id)


def _stored_request(record: StudioRunRecord) -> AssetRequest:
    """The request stored with the run, checked again against the schema."""
    try:
        return AssetRequest.model_validate(record.request)
    except ValidationError as e:
        raise StoredAssetRequestInvalid(record.id) from e


def _run_of_project(
    workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> Select[StudioRunRecord]:
    return (
        select(StudioRunRecord)
        .join(Project, Project.id == StudioRunRecord.project_id)
        .where(
            Project.workspace_id == workspace_id,
            Project.id == project_id,
            StudioRunRecord.id == run_id,
        )
    )


def _found_run(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> StudioRunRecord:
    record = session.scalar(_run_of_project(workspace_id, project_id, run_id))
    if record is None:
        raise StudioRunNotFound(project_id, run_id)
    return record


def _run_to_change(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> StudioRunRecord:
    """The run, with its row held until the transaction ends.

    Two reports about one run then apply one after the other, and the second sees what the
    first recorded.
    """
    record = session.scalar(
        _run_of_project(workspace_id, project_id, run_id)
        .with_for_update(of=StudioRunRecord)
        # A row the session still holds from before it waited may have changed since.
        .execution_options(populate_existing=True)
    )
    if record is None:
        raise StudioRunNotFound(project_id, run_id)
    return record


def _change(session: Session, record: StudioRunRecord, **columns: object) -> None:
    session.execute(
        update(StudioRunRecord).where(StudioRunRecord.id == record.id).values(**columns)
    )
    session.refresh(record)


def _studio_run(record: StudioRunRecord) -> StudioRun:
    return StudioRun(
        id=record.id,
        project_id=record.project_id,
        status=record.status,
        platform=Platform(record.platform),
        error=record.error,
        final_asset_id=record.final_asset_id,
        cost_usd=record.cost_usd,
        requested_by=record.requested_by,
        chat_thread_id=record.chat_thread_id,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )
