"""Proposals: the actions the policy gate held for a person, and what the person decided.

The worker stores a proposal when a run asks for an action that publishes or spends
(``agent_runtime/gate.py``); a person approves or rejects it through the API; the worker reads
the decision and runs the action or tells the graph it was refused. The row is the one record
of the decision: the signal the API sends only wakes the run's workflow, which reads it here.

A proposal moves forwards only, and once:

- stored: ``pending``, with a deadline. Storing the same action again (a retried activity)
  returns the proposal that is there and changes nothing;
- ``pending`` to ``approved`` or ``rejected``, by a person (``decide_proposal``), before the
  deadline. A proposal that is decided, or whose deadline passed, is refused
  (``ProposalNotPending``);
- ``pending`` to ``expired``, by the run (``settle_proposal``), when its deadline passed or the
  run ended while it waited.

Each of those transitions is recorded as an event of the run (``run_events.py``), so the
person watching it sees the action wait and how it ended; a call that changes nothing records
nothing.

Every function is scoped to a workspace and a project but two: ``list_waiting_proposals``,
which is the workspace's own view of what waits across its projects, and
``proposal_status_of_action``, with which the worker's ``run_action`` checks, from the action's
idempotency key alone, that an action held by the gate was approved before it runs it. The key
is the runtime's (``ActionRequest.action_id``), never a value a request chose. Functions take a
session and never commit.

This module decides nothing about which actions need a proposal (the gate does) or who may
decide (the API's identity does): the decider is whatever user id the caller authenticated.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from openmarketer_core.agent_runtime.actions import ActionEffect, ActionRequest
from openmarketer_core.agent_runtime.gate import ProposalStatus, RunScope
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.db.models import Project, ProposalRecord
from openmarketer_core.db.pages import CreatedPosition, Page, page_of, rows_to_fetch
from openmarketer_core.db.projects import get_project
from openmarketer_core.db.run_events import append_events
from openmarketer_core.run_events import (
    PROPOSAL_DECIDED,
    PROPOSAL_EXPIRED,
    PROPOSAL_WAITING,
    RunEvent,
)


class ProposalNotFound(Exception):
    """The workspace has no such proposal in such a project."""

    def __init__(self, project_id: uuid.UUID, proposal: uuid.UUID | str) -> None:
        super().__init__(f"project {project_id} has no proposal {proposal}")


class ProposalNotPending(Exception):
    """The proposal was decided already, or its deadline passed: it is decided once."""

    def __init__(self, proposal_id: uuid.UUID, status: ProposalStatus) -> None:
        super().__init__(f"proposal {proposal_id} is {status.value} and can no longer be decided")
        self.status = status


@dataclass(frozen=True)
class ActionProposal:
    """One action held for a person: what was asked, by which run, and where it stands."""

    id: uuid.UUID
    project_id: uuid.UUID
    run_kind: str
    run_id: uuid.UUID
    workflow_id: str
    action_name: str
    effect: ActionEffect
    payload: dict[str, Any]
    status: ProposalStatus
    created_at: datetime
    expires_at: datetime
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    # What an action that spends was estimated to cost, in US dollars; None for any other.
    estimate_usd: Decimal | None = None


@dataclass(frozen=True)
class PendingProposalsSummary:
    """The proposals of a project that wait for a person: how many, and the nearest deadline."""

    count: int
    next_expiry: datetime | None


def pending_proposals_summary(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> PendingProposalsSummary:
    """How many of the project's proposals wait for a person, and when the next one expires.

    Raises ``ProjectNotFound`` when the workspace has no such project. The nearest deadline is
    ``None`` when none wait.
    """
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    count, next_expiry = session.execute(
        select(func.count(), func.min(ProposalRecord.expires_at))
        .select_from(ProposalRecord)
        .join(Project, Project.id == ProposalRecord.project_id)
        .where(
            Project.workspace_id == workspace_id,
            Project.id == project_id,
            ProposalRecord.status == ProposalStatus.PENDING,
        )
    ).one()
    return PendingProposalsSummary(count=count, next_expiry=next_expiry)


def propose_action(
    session: Session,
    *,
    scope: RunScope,
    run: GraphRunRef,
    workflow_id: str,
    request: ActionRequest,
    effect: ActionEffect,
    wait_seconds: float,
    estimate_usd: Decimal | None = None,
) -> ActionProposal:
    """Store ``request`` as a pending proposal, or return the one stored for it before.

    A proposal stored now is recorded as the run's ``proposal_waiting`` event; one that was
    already there records nothing. The deadline is ``wait_seconds`` from now, by the database's
    clock. ``estimate_usd`` is what an action that spends is estimated to cost, shown to the
    person who decides. Raises ``ProjectNotFound`` when the workspace has no such project, and
    ``ProposalNotFound`` when the action's key is held by a proposal of another project.
    """
    get_project(session, workspace_id=scope.workspace_id, project_id=scope.project_id)
    # RETURNING answers whether the row was inserted: a conflicting insert returns no row.
    stored_now = (
        session.scalar(
            insert(ProposalRecord)
            .values(
                project_id=scope.project_id,
                run_kind=run.kind,
                run_id=run.run_id,
                workflow_id=workflow_id,
                action_id=request.action_id,
                action_name=request.name,
                effect=effect,
                payload=request.payload,
                estimate_usd=estimate_usd,
                expires_at=func.now() + timedelta(seconds=wait_seconds),
            )
            .on_conflict_do_nothing(index_elements=[ProposalRecord.action_id])
            .returning(ProposalRecord.id)
        )
        is not None
    )
    record = session.scalar(
        _proposals_of_project(scope.workspace_id, scope.project_id).where(
            ProposalRecord.action_id == request.action_id
        )
    )
    if record is None:
        raise ProposalNotFound(scope.project_id, request.action_id)
    if stored_now:
        _record_on_run(
            session,
            workspace_id=scope.workspace_id,
            record=record,
            event=RunEvent(
                PROPOSAL_WAITING,
                {
                    "proposal_id": str(record.id),
                    "action_name": record.action_name,
                    "effect": record.effect.value,
                },
            ),
        )
    return _proposal(record)


def decide_proposal(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    proposal_id: uuid.UUID,
    approved: bool,
    decided_by: uuid.UUID,
) -> ActionProposal:
    """Record that ``decided_by`` approved or rejected a pending proposal.

    Raises ``ProposalNotFound`` and, for a proposal that was decided or whose deadline passed,
    ``ProposalNotPending``: a late decision changes nothing, and the run expires the proposal.
    The deadline is compared with the time of this statement, not of the transaction, which
    may have begun long before.
    """
    record = _proposal_to_change(
        session,
        _proposals_of_project(workspace_id, project_id).where(ProposalRecord.id == proposal_id),
        project_id,
        proposal_id,
    )
    if record.status is not ProposalStatus.PENDING:
        raise ProposalNotPending(proposal_id, record.status)
    if session.execute(select(func.statement_timestamp())).scalar_one() >= record.expires_at:
        raise ProposalNotPending(proposal_id, ProposalStatus.EXPIRED)
    _change(
        session,
        record,
        status=ProposalStatus.APPROVED if approved else ProposalStatus.REJECTED,
        decided_by=decided_by,
        decided_at=func.now(),
    )
    _record_on_run(
        session,
        workspace_id=workspace_id,
        record=record,
        event=RunEvent(PROPOSAL_DECIDED, {"proposal_id": str(record.id), "approved": approved}),
    )
    return _proposal(record)


def settle_proposal(
    session: Session, *, scope: RunScope, action_id: str, deadline_passed: bool
) -> ProposalStatus:
    """Where the proposal of ``action_id`` stands, expiring it first if it is pending and
    ``deadline_passed``.

    The run says whether the deadline passed, by its own timer, so that the proposal ends when
    the run stops waiting for it. Raises ``ProposalNotFound``.
    """
    record = _proposal_to_change(
        session,
        _proposals_of_project(scope.workspace_id, scope.project_id).where(
            ProposalRecord.action_id == action_id
        ),
        scope.project_id,
        action_id,
    )
    if record.status is ProposalStatus.PENDING and deadline_passed:
        _change(session, record, status=ProposalStatus.EXPIRED, decided_at=func.now())
        _record_on_run(
            session,
            workspace_id=scope.workspace_id,
            record=record,
            event=RunEvent(PROPOSAL_EXPIRED, {"proposal_id": str(record.id)}),
        )
    return record.status


def proposal_status_of_action(session: Session, action_id: str) -> ProposalStatus | None:
    """Where the proposal of ``action_id`` stands, or ``None`` when the action has none.

    Not scoped, as the module's docstring says: it is how ``run_action`` checks an approval.
    """
    return session.scalar(
        select(ProposalRecord.status).where(ProposalRecord.action_id == action_id)
    )


def proposal_of_action(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, action_id: str
) -> uuid.UUID | None:
    """The id of the proposal the gate held ``action_id`` as, or ``None`` when it held none.

    An action of another project or workspace is treated as one with no proposal. It is how a
    handler that runs once its action was approved names the proposal in what it records.
    """
    return session.scalar(
        _proposals_of_project(workspace_id, project_id)
        .where(ProposalRecord.action_id == action_id)
        .with_only_columns(ProposalRecord.id)
    )


def pending_proposal_of_run(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_kind: str,
    run_id: uuid.UUID,
) -> uuid.UUID | None:
    """The id of the proposal run ``run_id`` of ``run_kind`` waits on, or ``None``.

    A run waits on one held action at a time, so it has at most one pending proposal; the
    newest is answered should it have more. A run of another project or workspace is treated
    as one that waits on nothing.
    """
    return session.scalar(
        _proposals_of_project(workspace_id, project_id)
        .where(
            ProposalRecord.run_kind == run_kind,
            ProposalRecord.run_id == run_id,
            ProposalRecord.status == ProposalStatus.PENDING,
        )
        .order_by(ProposalRecord.created_at.desc(), ProposalRecord.id.desc())
        .limit(1)
        .with_only_columns(ProposalRecord.id)
    )


def get_proposal(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, proposal_id: uuid.UUID
) -> ActionProposal:
    """The proposal as it is now. Raises ``ProposalNotFound``."""
    record = session.scalar(
        _proposals_of_project(workspace_id, project_id).where(ProposalRecord.id == proposal_id)
    )
    if record is None:
        raise ProposalNotFound(project_id, proposal_id)
    return _proposal(record)


def list_proposals(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    limit: int,
    status: ProposalStatus | None = None,
    before: CreatedPosition | None = None,
) -> Page[ActionProposal, CreatedPosition]:
    """The project's proposals, newest first, only those with ``status`` when it is given.

    At most ``limit`` of them, starting after ``before`` (the ``next_before`` of the previous
    page) or at the newest. Raises ``ProjectNotFound`` when the workspace has no such project.
    """
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    query = _proposals_of_project(workspace_id, project_id)
    if status is not None:
        query = query.where(ProposalRecord.status == status)
    if before is not None:
        query = query.where(
            tuple_(ProposalRecord.created_at, ProposalRecord.id)
            < tuple_(before.created_at, before.id)
        )
    records = session.scalars(
        query.order_by(ProposalRecord.created_at.desc(), ProposalRecord.id.desc()).limit(
            rows_to_fetch(limit)
        )
    )
    proposals = [_proposal(record) for record in records]
    return page_of(proposals, limit, lambda last: CreatedPosition(last.created_at, last.id))


def list_waiting_proposals(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    limit: int,
    before: CreatedPosition | None = None,
) -> Page[ActionProposal, CreatedPosition]:
    """The workspace's pending proposals, newest first, across all of its projects.

    At most ``limit`` of them, starting after ``before`` (the ``next_before`` of the previous
    page) or at the newest. This is the workspace's own view of what waits for a person, with
    which the dashboard tells them wherever they are; a project's view is ``list_proposals``.
    """
    query = (
        select(ProposalRecord)
        .join(Project, Project.id == ProposalRecord.project_id)
        .where(
            Project.workspace_id == workspace_id,
            ProposalRecord.status == ProposalStatus.PENDING,
        )
    )
    if before is not None:
        query = query.where(
            tuple_(ProposalRecord.created_at, ProposalRecord.id)
            < tuple_(before.created_at, before.id)
        )
    records = session.scalars(
        query.order_by(ProposalRecord.created_at.desc(), ProposalRecord.id.desc()).limit(
            rows_to_fetch(limit)
        )
    )
    proposals = [_proposal(record) for record in records]
    return page_of(proposals, limit, lambda last: CreatedPosition(last.created_at, last.id))


def _proposals_of_project(workspace_id: uuid.UUID, project_id: uuid.UUID) -> Select[ProposalRecord]:
    return (
        select(ProposalRecord)
        .join(Project, Project.id == ProposalRecord.project_id)
        .where(Project.workspace_id == workspace_id, Project.id == project_id)
    )


def _record_on_run(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    record: ProposalRecord,
    event: RunEvent,
) -> None:
    """Record ``event`` as one of the proposal's run's events, for the person watching it."""
    append_events(
        session,
        workspace_id=workspace_id,
        project_id=record.project_id,
        run_kind=record.run_kind,
        run_id=record.run_id,
        events=[event],
    )


def _proposal_to_change(
    session: Session,
    query: Select[ProposalRecord],
    project_id: uuid.UUID,
    named: uuid.UUID | str,
) -> ProposalRecord:
    """The proposal, with its row held until the transaction ends.

    A person's decision and the run's expiry then apply one after the other, and the second
    sees what the first recorded.
    """
    record = session.scalar(
        query.with_for_update(of=ProposalRecord)
        # A row the session still holds from before it waited may have changed since.
        .execution_options(populate_existing=True)
    )
    if record is None:
        raise ProposalNotFound(project_id, named)
    return record


def _change(session: Session, record: ProposalRecord, **columns: object) -> None:
    session.execute(update(ProposalRecord).where(ProposalRecord.id == record.id).values(**columns))
    session.refresh(record)


def _proposal(record: ProposalRecord) -> ActionProposal:
    return ActionProposal(
        id=record.id,
        project_id=record.project_id,
        run_kind=record.run_kind,
        run_id=record.run_id,
        workflow_id=record.workflow_id,
        action_name=record.action_name,
        effect=record.effect,
        payload=record.payload,
        status=record.status,
        created_at=record.created_at,
        expires_at=record.expires_at,
        decided_by=record.decided_by,
        decided_at=record.decided_at,
        estimate_usd=record.estimate_usd,
    )
