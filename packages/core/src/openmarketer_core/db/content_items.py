"""Content items: the post each writing run proposed, how it was written, and how it ended.

A content item is the record of one post: its platform, the text as it would be sent, the
other variants, the plan it was drafted from and what code changed in it, the claims with the
features they rest on, the code rules' and the critic's reports, the numbers the profile does
not hold, the profile version, the proposal and what the run's model calls cost. The reports
and the plan are JSON the worker wrote; this module stores and returns them and reads no rule
in them.

A run has one item. The worker stores it, ``proposed``, while the run is running and after the
run chose its profile version (``store_proposed_content_item``); storing it again for the same
run with the same text gives back the stored item, so a retried activity stores one. The item
is then settled once (``settle_content_item``): ``dry_run`` when the person approved it and the
post was recorded as it would have been sent, ``rejected``, ``expired``, or ``failed``. Settling
it the same way again changes nothing; another way is ``ContentItemSettled``. An item still
proposed when its run fails is settled ``failed`` by the run (``db/writing_runs.py``). A
finished run's item is never changed.

The proposal is the one the gate held the run's ``publish_post`` as, and must be a proposal of
the same run. It is named when the item is stored or when it is settled, whichever comes first
knows it, and once.

Every function is scoped to a workspace and a project, takes a session and never commits; an
item of another workspace or project is treated as missing. Writes take the workspace lock
and then hold the run's row, so an item cannot be stored or settled while the run ends. Items
are deleted with their project, never on their own (``db/project_removal.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select, tuple_, update
from sqlalchemy.orm import Session

from openmarketer_core.db.models import (
    ContentItemRecord,
    ContentItemStatus,
    ProductProfileRecord,
    Project,
    ProposalRecord,
    WritingRunRecord,
    WritingRunStatus,
)
from openmarketer_core.db.pages import CreatedPosition, Page, page_of, rows_to_fetch
from openmarketer_core.db.projects import get_project
from openmarketer_core.db.workspace_lock import wait_for_other_writes
from openmarketer_core.db.writing_runs import (
    FINISHED,
    WritingRunFinished,
    WritingRunNotFound,
    WritingRunNotStarted,
)
from openmarketer_core.writing.brief import WritingPlatform
from openmarketer_core.writing.workflow_contract import WRITING_RUN_KIND

# The ways an item ends; ``proposed`` is how it starts.
SETTLED = (
    ContentItemStatus.DRY_RUN,
    ContentItemStatus.REJECTED,
    ContentItemStatus.EXPIRED,
    ContentItemStatus.FAILED,
)


class ContentItemNotFound(Exception):
    """The workspace has no such content item of such a project."""

    def __init__(self, project_id: uuid.UUID, item_id: uuid.UUID) -> None:
        super().__init__(f"project {project_id} has no content item {item_id}")


class ContentItemStored(Exception):
    """The run stored another item before; a run proposes one post."""

    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(f"writing run {run_id} has already stored another content item")


class ContentItemSettled(Exception):
    """The item was settled before, another way; it is settled once."""

    def __init__(self, item_id: uuid.UUID, status: ContentItemStatus) -> None:
        self.status = status
        super().__init__(f"content item {item_id} has already been settled: {status.value}")


class ProfileVersionNotChosen(Exception):
    """The run has not recorded the profile version it writes from; its item needs one."""

    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(f"writing run {run_id} has not chosen its profile version")


class ProposalOfAnotherRun(Exception):
    """The proposal is not one of the run's, or the item names another one already."""

    def __init__(self, run_id: uuid.UUID, proposal_id: uuid.UUID) -> None:
        super().__init__(f"proposal {proposal_id} is not the proposal of writing run {run_id}")


@dataclass(frozen=True)
class NewContentItem:
    """A proposed post as the worker stores it; the reports are JSON-ready values."""

    text: str
    plan: Mapping[str, Any]
    check_report: Mapping[str, Any]
    critic_report: Mapping[str, Any]
    cost_usd: Decimal
    plan_changes: Sequence[Any] = ()
    alternatives: Sequence[Any] = ()
    claims: Sequence[Any] = ()
    flagged_numbers: Sequence[Any] = ()
    proposal_id: uuid.UUID | None = None


@dataclass(frozen=True)
class ContentItem:
    """One post a run proposed, with the record of how it was written and how it ended."""

    id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    platform: WritingPlatform
    text: str
    status: ContentItemStatus
    plan: Mapping[str, Any]
    plan_changes: tuple[Any, ...]
    alternatives: tuple[Any, ...]
    claims: tuple[Any, ...]
    check_report: Mapping[str, Any]
    critic_report: Mapping[str, Any]
    flagged_numbers: tuple[Any, ...]
    profile_version_id: uuid.UUID
    profile_version: int  # the number of that version, which is what a person is shown
    proposal_id: uuid.UUID | None
    cost_usd: Decimal
    created_at: datetime
    settled_at: datetime | None


def store_proposed_content_item(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    item: NewContentItem,
) -> ContentItem:
    """Store the post the run proposes, ``proposed``, and name it as the run's item.

    The item takes the run's platform and profile version. Storing it again with the same text
    gives back the stored item; another text is ``ContentItemStored``. Raises
    ``WritingRunNotFound``, ``WritingRunNotStarted``, ``WritingRunFinished``,
    ``ProfileVersionNotChosen`` and ``ProposalOfAnotherRun``. Takes the workspace lock.
    """
    wait_for_other_writes(session, workspace_id)
    run = _running_run(session, workspace_id, project_id, run_id)
    if run.content_item_id is not None:
        stored = _found_item(session, workspace_id, project_id, run.content_item_id)
        if stored.text != item.text:
            raise ContentItemStored(run_id)
        return _content_item(session, stored)
    if run.profile_version_id is None:
        raise ProfileVersionNotChosen(run_id)
    if item.proposal_id is not None:
        _require_proposal_of_run(session, project_id, run_id, item.proposal_id)
    record = ContentItemRecord(
        project_id=project_id,
        run_id=run_id,
        platform=run.platform,
        text=item.text,
        plan=dict(item.plan),
        plan_changes=list(item.plan_changes),
        alternatives=list(item.alternatives),
        claims=list(item.claims),
        check_report=dict(item.check_report),
        critic_report=dict(item.critic_report),
        flagged_numbers=list(item.flagged_numbers),
        profile_version_id=run.profile_version_id,
        proposal_id=item.proposal_id,
        cost_usd=item.cost_usd,
    )
    session.add(record)
    session.flush()
    session.execute(
        update(WritingRunRecord)
        .where(WritingRunRecord.id == run_id)
        .values(content_item_id=record.id)
    )
    session.refresh(record)
    return _content_item(session, record)


def settle_content_item(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    item_id: uuid.UUID,
    status: ContentItemStatus,
    proposal_id: uuid.UUID | None = None,
) -> ContentItem:
    """Record how the proposed item ended, while its run is running.

    ``status`` is one of ``SETTLED`` (``ValueError`` otherwise). Settling it the same way again
    changes nothing; another way is ``ContentItemSettled``. ``proposal_id``, when given, is
    recorded if the item names no proposal yet and must be the one it names otherwise. Raises
    ``ContentItemNotFound``, ``WritingRunFinished`` and ``ProposalOfAnotherRun``. Takes the
    workspace lock.
    """
    if status not in SETTLED:
        raise ValueError(f"an item is settled as one of {[s.value for s in SETTLED]}")
    wait_for_other_writes(session, workspace_id)
    record = _found_item(session, workspace_id, project_id, item_id)
    _running_run(session, workspace_id, project_id, record.run_id)
    record = _found_item(session, workspace_id, project_id, item_id, for_update=True)
    if proposal_id is not None and record.proposal_id != proposal_id:
        if record.proposal_id is not None:
            raise ProposalOfAnotherRun(record.run_id, proposal_id)
        _require_proposal_of_run(session, project_id, record.run_id, proposal_id)
    if record.status is not ContentItemStatus.PROPOSED and record.status is not status:
        raise ContentItemSettled(item_id, record.status)
    columns: dict[str, object] = {}
    if record.status is ContentItemStatus.PROPOSED:
        columns.update(status=status, settled_at=func.now())
    if proposal_id is not None and record.proposal_id is None:
        columns["proposal_id"] = proposal_id
    if columns:
        session.execute(
            update(ContentItemRecord).where(ContentItemRecord.id == item_id).values(**columns)
        )
        session.refresh(record)
    return _content_item(session, record)


def content_item(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, item_id: uuid.UUID
) -> ContentItem:
    """One content item of the project as it is now."""
    return _content_item(session, _found_item(session, workspace_id, project_id, item_id))


def list_content_items(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    limit: int,
    before: CreatedPosition | None = None,
) -> Page[ContentItem, CreatedPosition]:
    """The project's content items, newest first.

    At most ``limit`` of them, starting after ``before`` (the ``next_before`` of the previous
    page) or at the newest. Raises ``ProjectNotFound`` when the workspace has no such project.
    """
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    query = _items_of_project(workspace_id, project_id).add_columns(ProductProfileRecord.version)
    query = query.join(
        ProductProfileRecord, ProductProfileRecord.id == ContentItemRecord.profile_version_id
    )
    if before is not None:
        query = query.where(
            tuple_(ContentItemRecord.created_at, ContentItemRecord.id)
            < tuple_(before.created_at, before.id)
        )
    rows = session.execute(
        query.order_by(ContentItemRecord.created_at.desc(), ContentItemRecord.id.desc()).limit(
            rows_to_fetch(limit)
        )
    )
    items = [_item_of_version(record, version) for record, version in rows]
    return page_of(items, limit, lambda last: CreatedPosition(last.created_at, last.id))


def _running_run(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> WritingRunRecord:
    """The run, held until the transaction ends, so that it cannot end while its item changes."""
    record = session.scalar(
        select(WritingRunRecord)
        .join(Project, Project.id == WritingRunRecord.project_id)
        .where(
            Project.workspace_id == workspace_id,
            Project.id == project_id,
            WritingRunRecord.id == run_id,
        )
        .with_for_update(of=WritingRunRecord)
        .execution_options(populate_existing=True)
    )
    if record is None:
        raise WritingRunNotFound(project_id, run_id)
    if record.status is WritingRunStatus.QUEUED:
        raise WritingRunNotStarted(run_id)
    if record.status in FINISHED:
        raise WritingRunFinished(run_id, record.status)
    return record


def _require_proposal_of_run(
    session: Session, project_id: uuid.UUID, run_id: uuid.UUID, proposal_id: uuid.UUID
) -> None:
    found = session.scalar(
        select(ProposalRecord.id).where(
            ProposalRecord.id == proposal_id,
            ProposalRecord.project_id == project_id,
            ProposalRecord.run_kind == WRITING_RUN_KIND,
            ProposalRecord.run_id == run_id,
        )
    )
    if found is None:
        raise ProposalOfAnotherRun(run_id, proposal_id)


def _items_of_project(workspace_id: uuid.UUID, project_id: uuid.UUID) -> Select[ContentItemRecord]:
    return (
        select(ContentItemRecord)
        .join(Project, Project.id == ContentItemRecord.project_id)
        .where(Project.workspace_id == workspace_id, ContentItemRecord.project_id == project_id)
    )


def _found_item(
    session: Session,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    item_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> ContentItemRecord:
    query = _items_of_project(workspace_id, project_id).where(ContentItemRecord.id == item_id)
    if for_update:
        query = query.with_for_update(of=ContentItemRecord).execution_options(
            populate_existing=True
        )
    record = session.scalar(query)
    if record is None:
        raise ContentItemNotFound(project_id, item_id)
    return record


def _content_item(session: Session, record: ContentItemRecord) -> ContentItem:
    # The item's foreign key keeps its version for as long as the item exists.
    version = session.execute(
        select(ProductProfileRecord.version).where(
            ProductProfileRecord.id == record.profile_version_id
        )
    ).scalar_one()
    return _item_of_version(record, version)


def _item_of_version(record: ContentItemRecord, profile_version: int) -> ContentItem:
    return ContentItem(
        id=record.id,
        project_id=record.project_id,
        run_id=record.run_id,
        platform=WritingPlatform(record.platform),
        text=record.text,
        status=record.status,
        plan=dict(record.plan),
        plan_changes=tuple(record.plan_changes),
        alternatives=tuple(record.alternatives),
        claims=tuple(record.claims),
        check_report=dict(record.check_report),
        critic_report=dict(record.critic_report),
        flagged_numbers=tuple(record.flagged_numbers),
        profile_version_id=record.profile_version_id,
        profile_version=profile_version,
        proposal_id=record.proposal_id,
        cost_usd=record.cost_usd,
        created_at=record.created_at,
        settled_at=record.settled_at,
    )
