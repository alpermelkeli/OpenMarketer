"""Reserving what an action is estimated to spend, then committing or releasing it.

The design report's section "Budget controls": before an action spends, its
estimate is *reserved* in a serialisable PostgreSQL transaction, then committed
or released, so that concurrent actions cannot jointly exceed the cap. The check
and the reservation are one SERIALIZABLE transaction (``db/session.py``,
``serializable``), tried again when PostgreSQL could not serialise it: of two
reservations that together would pass the cap, the second sees the first and is
refused.

What each decision of the rule (``caps.py``) does here:

- ``RUN_AUTOMATICALLY``: the estimate is held, and the action runs;
- ``ASK_A_PERSON``: the estimate is held while a person decides, so that a spend
  waiting for approval keeps its place under the cap; a rejection releases it;
- ``REFUSE``: nothing is written, and the action does not run.

An action is named by its run's kind and its idempotency key
(``ActionRequest.action_id``). Every use case is idempotent on it: a retried
reservation finds the one made before, with the decision it was made with and
its amount, whatever it is asked this time; a retried commit or release finds
the settlement there and changes nothing. A refused spend wrote nothing, so a
retry decides it again. The cap is per workspace and calendar month in UTC,
and an action counts in the month it was reserved in.

The writing use cases receive the session factory, since they own their
transaction; ``spent_this_month`` reads in the caller's. None reads the
environment: the caps are the caller's (``workspace_settings/creative_budget_settings.py``).
Whether an action needs a reservation at all, and what it is estimated to cost,
are its workflow's.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.budget.caps import CreativeCaps, SpendDecision, decide_spend, usd
from openmarketer_core.db.budget_ledger import (
    LedgerReservation,
    month_balance,
    record_reservation,
    record_settlement,
    reservation_of_action,
)
from openmarketer_core.db.models import LedgerEntryKind
from openmarketer_core.db.projects import get_project
from openmarketer_core.db.session import serializable
from openmarketer_core.db.workspace_lock import wait_for_other_writes


class ReservationNotFound(Exception):
    """The workspace holds no reservation for the action."""

    def __init__(self, run_kind: str, action_id: str) -> None:
        super().__init__(f"no spend is reserved for action {action_id} of a {run_kind} run")


class SpendAlreadySettled(Exception):
    """The reservation was settled the other way: a released spend is not committed, nor a
    committed one released."""

    def __init__(self, action_id: str, settlement: LedgerEntryKind) -> None:
        done = "committed" if settlement is LedgerEntryKind.COMMIT else "released"
        super().__init__(f"the spend of action {action_id} was {done} already")
        self.settlement = settlement


@dataclass(frozen=True)
class Reservation:
    """What the ledger decided for one action, and where its spend stands.

    ``reserved`` says whether the ledger holds a reservation for it: true for both
    decisions that let it run or wait, false for ``REFUSE``. ``amount_usd`` is the
    estimate that was decided on; ``settlement`` is ``COMMIT`` or ``RELEASE`` once the
    spend is settled, with ``settled_usd`` what was spent (zero for a release).
    """

    run_kind: str
    action_id: str
    decision: SpendDecision
    reserved: bool
    amount_usd: Decimal
    settlement: LedgerEntryKind | None = None
    settled_usd: Decimal | None = None


@dataclass(frozen=True)
class MonthlySpend:
    """The workspace's spending this calendar month (UTC), for the dashboard.

    ``remaining_usd`` is what can still be reserved, never below zero: an action that
    spent more than its estimate can carry the month past its cap.
    """

    month_start: datetime
    spent_usd: Decimal
    reserved_usd: Decimal
    cap_usd: Decimal

    @property
    def remaining_usd(self) -> Decimal:
        return max(Decimal(0), self.cap_usd - self.spent_usd - self.reserved_usd)


def reserve_spend(
    sessions: sessionmaker[Session],
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_kind: str,
    run_id: uuid.UUID,
    action_id: str,
    estimate_usd: Decimal,
    caps: CreativeCaps,
) -> Reservation:
    """Decide the action's spend under ``caps`` and hold its estimate unless it is refused.

    Committed before it returns. Raises ``ProjectNotFound`` when the workspace has no such
    project, ``ReservationNotFound`` when the action's key is held by a reservation of
    another workspace, ``InvalidAmount`` for an estimate that is not one, and
    ``DatabaseError``.
    """
    estimate = usd(estimate_usd, "the estimate")

    def reserve(session: Session) -> Reservation:
        # Under SERIALIZABLE the lock is not what keeps the cap: it lines reservations up, and
        # one whose snapshot is older than the lock it waited for is tried again.
        wait_for_other_writes(session, workspace_id)
        get_project(session, workspace_id=workspace_id, project_id=project_id)
        found = reservation_of_action(
            session, workspace_id=workspace_id, run_kind=run_kind, action_id=action_id
        )
        if found is not None:
            return _of_ledger(found)
        balance = month_balance(session, workspace_id=workspace_id)
        decision = decide_spend(estimate, balance.spent_usd, balance.reserved_usd, caps)
        if decision is SpendDecision.REFUSE:
            return Reservation(run_kind, action_id, decision, reserved=False, amount_usd=estimate)
        made = record_reservation(
            session,
            workspace_id=workspace_id,
            project_id=project_id,
            run_kind=run_kind,
            run_id=run_id,
            action_id=action_id,
            amount_usd=estimate,
            decision=decision,
        )
        if made is None:
            raise ReservationNotFound(run_kind, action_id)
        return _of_ledger(made)

    return serializable(sessions, reserve)


def commit_spend(
    sessions: sessionmaker[Session],
    *,
    workspace_id: uuid.UUID,
    run_kind: str,
    action_id: str,
    actual_usd: Decimal,
) -> Reservation:
    """Record that the action spent ``actual_usd``, which may differ from its estimate.

    A spend committed before keeps the amount it was committed with. Raises
    ``ReservationNotFound``, ``SpendAlreadySettled`` when the spend was released,
    ``InvalidAmount`` and ``DatabaseError``.
    """
    actual = usd(actual_usd, "the amount spent")
    return _settle(sessions, workspace_id, run_kind, action_id, LedgerEntryKind.COMMIT, actual)


def release_spend(
    sessions: sessionmaker[Session], *, workspace_id: uuid.UUID, run_kind: str, action_id: str
) -> Reservation:
    """Give back what is held for the action: it did not run, or a person rejected it.

    Raises ``ReservationNotFound``, ``SpendAlreadySettled`` when the spend was committed,
    and ``DatabaseError``.
    """
    return _settle(sessions, workspace_id, run_kind, action_id, LedgerEntryKind.RELEASE, Decimal(0))


def spent_this_month(
    session: Session, *, workspace_id: uuid.UUID, caps: CreativeCaps
) -> MonthlySpend:
    """What the workspace spent and holds this calendar month (UTC), against its cap."""
    balance = month_balance(session, workspace_id=workspace_id)
    return MonthlySpend(
        month_start=balance.month_start,
        spent_usd=balance.spent_usd,
        reserved_usd=balance.reserved_usd,
        cap_usd=caps.monthly_usd,
    )


def _settle(
    sessions: sessionmaker[Session],
    workspace_id: uuid.UUID,
    run_kind: str,
    action_id: str,
    settlement: LedgerEntryKind,
    amount_usd: Decimal,
) -> Reservation:
    def settle(session: Session) -> Reservation:
        wait_for_other_writes(session, workspace_id)
        found = reservation_of_action(
            session, workspace_id=workspace_id, run_kind=run_kind, action_id=action_id
        )
        if found is None:
            raise ReservationNotFound(run_kind, action_id)
        if found.settlement is not None and found.settlement is not settlement:
            raise SpendAlreadySettled(action_id, found.settlement)
        if found.settlement is None:
            found = record_settlement(
                session, reservation=found, settlement=settlement, amount_usd=amount_usd
            )
        return _of_ledger(found)

    return serializable(sessions, settle)


def _of_ledger(entry: LedgerReservation) -> Reservation:
    return Reservation(
        run_kind=entry.run_kind,
        action_id=entry.action_id,
        decision=entry.decision,
        reserved=True,
        amount_usd=entry.amount_usd,
        settlement=entry.settlement,
        settled_usd=entry.settled_usd,
    )
