"""Budget ledger store: the entries that hold, commit and release what an action spends.

An action is named by its run's kind and its idempotency key
(``ActionRequest.action_id``). It has one reservation, made with the decision of
the budget rule, and at most one settling entry: a commit of what it spent, or a
release of the hold. The ledger is append-only (a trigger refuses any change), so
every balance is derived here by query: what an action spent is its commit, what
is held is a reservation without a settling entry.

A month is a calendar month in UTC, by the database's clock, and an action counts
in the month it was reserved in: the month whose cap its decision was made
against, also when it is settled after the month ended.

Every function is scoped to a workspace, and a reservation of another workspace is
treated as missing, but one: ``held_decision_of_action``, with which the worker's
``run_action`` checks, from the run's kind and the action's idempotency key alone, that
an action that spends holds a reservation before its handler runs. The key is the
runtime's (``ActionRequest.action_id``), never a value a request chose, and the kind is
the agent's own. Functions take a session, flush and never commit; whether
the caller's transaction is SERIALIZABLE, which keeps two reservations from
jointly passing the cap, is the caller's (``budget/ledger.py``). Callers get
frozen dataclasses, never the ORM row.

This module decides nothing: the rule is ``budget/caps.py``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from openmarketer_core.budget.caps import SpendDecision
from openmarketer_core.db.models import BudgetLedgerEntryRecord, LedgerEntryKind

SETTLING = (LedgerEntryKind.COMMIT, LedgerEntryKind.RELEASE)

# The ledger twice over: an action's reservation, and the entry that settles it.
_reserve = aliased(BudgetLedgerEntryRecord, name="reserve")
_settle = aliased(BudgetLedgerEntryRecord, name="settle")
_SETTLES = (
    (_settle.workspace_id == _reserve.workspace_id)
    & (_settle.run_kind == _reserve.run_kind)
    & (_settle.action_id == _reserve.action_id)
    & _settle.entry_kind.in_(SETTLING)
)


@dataclass(frozen=True)
class LedgerReservation:
    """An action's reservation and, once it is settled, how."""

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_kind: str
    run_id: uuid.UUID
    action_id: str
    amount_usd: Decimal
    decision: SpendDecision
    reserved_at: datetime
    settlement: LedgerEntryKind | None
    settled_usd: Decimal | None


@dataclass(frozen=True)
class MonthBalance:
    """What the workspace's actions reserved this month spent, and what they still hold."""

    month_start: datetime
    spent_usd: Decimal
    reserved_usd: Decimal


def reservation_of_action(
    session: Session, *, workspace_id: uuid.UUID, run_kind: str, action_id: str
) -> LedgerReservation | None:
    """The action's reservation in the workspace, with its settlement, or ``None``."""
    row = session.execute(
        select(_reserve, _settle.entry_kind, _settle.amount_usd)
        .outerjoin(_settle, _SETTLES)
        .where(
            _reserve.workspace_id == workspace_id,
            _reserve.run_kind == run_kind,
            _reserve.action_id == action_id,
            _reserve.entry_kind == LedgerEntryKind.RESERVE,
        )
    ).one_or_none()
    if row is None:
        return None
    record, settlement, settled_usd = row
    return _reservation(record, settlement, settled_usd)


def held_decision_of_action(
    session: Session, *, run_kind: str, action_id: str
) -> SpendDecision | None:
    """The decision the action's reservation was made with, while it is held and not settled.

    ``None`` when the action has no reservation, or its spend was committed or released.
    """
    return session.scalar(
        select(_reserve.decision)
        .outerjoin(_settle, _SETTLES)
        .where(
            _reserve.run_kind == run_kind,
            _reserve.action_id == action_id,
            _reserve.entry_kind == LedgerEntryKind.RESERVE,
            _settle.id.is_(None),
        )
    )


def month_balance(session: Session, *, workspace_id: uuid.UUID) -> MonthBalance:
    """The workspace's balance for the current calendar month (UTC), in one query."""
    # A constant beside the sums: it names no column, so it needs no GROUP BY.
    month_start = func.timezone("UTC", func.date_trunc("month", func.timezone("UTC", func.now())))
    spent = func.sum(_settle.amount_usd).filter(_settle.entry_kind == LedgerEntryKind.COMMIT)
    held = func.sum(_reserve.amount_usd).filter(_settle.id.is_(None))
    started, spent_usd, reserved_usd = session.execute(
        select(month_start, func.coalesce(spent, 0), func.coalesce(held, 0))
        .select_from(_reserve)
        .outerjoin(_settle, _SETTLES)
        .where(
            _reserve.workspace_id == workspace_id,
            _reserve.entry_kind == LedgerEntryKind.RESERVE,
            _reserve.created_at >= month_start,
        )
    ).one()
    return MonthBalance(
        month_start=started, spent_usd=Decimal(spent_usd), reserved_usd=Decimal(reserved_usd)
    )


def record_reservation(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_kind: str,
    run_id: uuid.UUID,
    action_id: str,
    amount_usd: Decimal,
    decision: SpendDecision,
) -> LedgerReservation | None:
    """Reserve ``amount_usd`` for the action, or find the reservation made for it before.

    ``None`` when the action's key is held by a reservation of another workspace.
    """
    session.execute(
        insert(BudgetLedgerEntryRecord)
        .values(
            workspace_id=workspace_id,
            project_id=project_id,
            run_kind=run_kind,
            run_id=run_id,
            action_id=action_id,
            entry_kind=LedgerEntryKind.RESERVE,
            amount_usd=amount_usd,
            decision=decision,
        )
        .on_conflict_do_nothing(
            index_elements=[BudgetLedgerEntryRecord.run_kind, BudgetLedgerEntryRecord.action_id],
            index_where=BudgetLedgerEntryRecord.entry_kind == LedgerEntryKind.RESERVE,
        )
    )
    return reservation_of_action(
        session, workspace_id=workspace_id, run_kind=run_kind, action_id=action_id
    )


def record_settlement(
    session: Session,
    *,
    reservation: LedgerReservation,
    settlement: LedgerEntryKind,
    amount_usd: Decimal,
) -> LedgerReservation:
    """Settle ``reservation`` by ``settlement``, unless it is settled already; as it is now.

    A release is recorded as zero, whatever ``amount_usd`` says: it spends nothing.
    """
    if settlement not in SETTLING:
        raise ValueError(f"{settlement.value} does not settle a reservation")
    session.execute(
        insert(BudgetLedgerEntryRecord)
        .values(
            workspace_id=reservation.workspace_id,
            project_id=reservation.project_id,
            run_kind=reservation.run_kind,
            run_id=reservation.run_id,
            action_id=reservation.action_id,
            entry_kind=settlement,
            amount_usd=amount_usd if settlement is LedgerEntryKind.COMMIT else Decimal(0),
        )
        .on_conflict_do_nothing(
            index_elements=[BudgetLedgerEntryRecord.run_kind, BudgetLedgerEntryRecord.action_id],
            index_where=BudgetLedgerEntryRecord.entry_kind.in_(SETTLING),
        )
    )
    settled = reservation_of_action(
        session,
        workspace_id=reservation.workspace_id,
        run_kind=reservation.run_kind,
        action_id=reservation.action_id,
    )
    if settled is None:
        raise LookupError(f"the reservation of {reservation.action_id} is gone from the ledger")
    return settled


def _reservation(
    record: BudgetLedgerEntryRecord,
    settlement: LedgerEntryKind | None,
    settled_usd: Decimal | None,
) -> LedgerReservation:
    if record.decision is None:
        raise LookupError(f"ledger entry {record.id} is not a reservation")
    return LedgerReservation(
        workspace_id=record.workspace_id,
        project_id=record.project_id,
        run_kind=record.run_kind,
        run_id=record.run_id,
        action_id=record.action_id,
        amount_usd=record.amount_usd,
        decision=record.decision,
        reserved_at=record.created_at,
        settlement=settlement,
        settled_usd=settled_usd,
    )
