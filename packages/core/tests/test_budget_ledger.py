"""Tests for the budget ledger: reserving a spend, committing or releasing it, the month's
balance, and what the database holds to. They need PostgreSQL (see the root ``conftest.py``).

The use cases own their transactions, so these tests commit into this module's own temporary
database; each test makes a workspace of its own.
"""

import threading
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.budget.caps import CreativeCaps, SpendDecision
from openmarketer_core.budget.ledger import (
    ReservationNotFound,
    SpendAlreadySettled,
    commit_spend,
    release_spend,
    reserve_spend,
    spent_this_month,
)
from openmarketer_core.db.budget_ledger import month_balance
from openmarketer_core.db.models import (
    BudgetLedgerEntryRecord,
    LedgerEntryKind,
    Project,
    Workspace,
)
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.session import DatabaseError, session_factory, transaction

CAPS = CreativeCaps(monthly_usd=Decimal(1), auto_per_action_usd=Decimal("0.25"))
RUN_KIND = "creative"


@dataclass(frozen=True)
class Scope:
    workspace_id: uuid.UUID
    project_id: uuid.UUID


@pytest.fixture
def sessions(engine) -> sessionmaker[Session]:
    return session_factory(engine.url.render_as_string(hide_password=False))


def new_scope(sessions: sessionmaker[Session]) -> Scope:
    with transaction(sessions) as session:
        workspace = Workspace(name="Acme")
        session.add(workspace)
        session.flush()
        project = Project(
            workspace_id=workspace.id, name="acme", source_repo_url="https://example.com/acme"
        )
        session.add(project)
        session.flush()
        return Scope(workspace.id, project.id)


@pytest.fixture
def scope(sessions) -> Scope:
    return new_scope(sessions)


@pytest.fixture
def action() -> str:
    """An action's key, unique like the runtime's: the tests share one database."""
    return f"generate-{uuid.uuid4().hex}"


def reserve(sessions, scope: Scope, estimate: str, action_id: str | None = None, caps=CAPS):
    return reserve_spend(
        sessions,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        run_kind=RUN_KIND,
        run_id=uuid.uuid4(),
        action_id=action_id or uuid.uuid4().hex,
        estimate_usd=Decimal(estimate),
        caps=caps,
    )


def commit(sessions, scope: Scope, action_id: str, actual: str):
    return commit_spend(
        sessions,
        workspace_id=scope.workspace_id,
        run_kind=RUN_KIND,
        action_id=action_id,
        actual_usd=Decimal(actual),
    )


def release(sessions, scope: Scope, action_id: str):
    return release_spend(
        sessions, workspace_id=scope.workspace_id, run_kind=RUN_KIND, action_id=action_id
    )


def balance(sessions, scope: Scope):
    with transaction(sessions) as session:
        return spent_this_month(session, workspace_id=scope.workspace_id, caps=CAPS)


def entries(sessions, scope: Scope) -> int:
    with transaction(sessions) as session:
        return (
            session.scalar(
                select(func.count())
                .select_from(BudgetLedgerEntryRecord)
                .where(BudgetLedgerEntryRecord.workspace_id == scope.workspace_id)
            )
            or 0
        )


def ledger_entry(scope: Scope, kind: LedgerEntryKind, **columns) -> BudgetLedgerEntryRecord:
    values = {
        "workspace_id": scope.workspace_id,
        "project_id": scope.project_id,
        "run_kind": RUN_KIND,
        "run_id": uuid.uuid4(),
        "action_id": f"generate-{uuid.uuid4().hex}",
        "entry_kind": kind,
        "amount_usd": Decimal("0.10"),
        "decision": SpendDecision.RUN_AUTOMATICALLY if kind is LedgerEntryKind.RESERVE else None,
    }
    return BudgetLedgerEntryRecord(**{**values, **columns})


def test_a_small_spend_runs_automatically_and_is_held(sessions, scope):
    reservation = reserve(sessions, scope, "0.20")

    assert reservation.decision is SpendDecision.RUN_AUTOMATICALLY
    assert reservation.reserved and reservation.amount_usd == Decimal("0.20")
    assert balance(sessions, scope).reserved_usd == Decimal("0.20")


def test_a_larger_spend_asks_a_person_and_is_held_while_it_waits(sessions, scope):
    reservation = reserve(sessions, scope, "0.50")

    assert reservation.decision is SpendDecision.ASK_A_PERSON
    assert reservation.reserved
    assert balance(sessions, scope).reserved_usd == Decimal("0.50")


def test_a_refused_spend_writes_nothing(sessions, scope):
    reservation = reserve(sessions, scope, "1.50")

    assert reservation.decision is SpendDecision.REFUSE
    assert not reservation.reserved
    assert entries(sessions, scope) == 0


def test_what_is_held_counts_against_the_cap(sessions, scope):
    reserve(sessions, scope, "0.60")

    assert reserve(sessions, scope, "0.60").decision is SpendDecision.REFUSE


def test_a_retried_reservation_finds_the_same_decision(action, sessions, scope):
    first = reserve(sessions, scope, "0.20", action_id=action)
    again = reserve(sessions, scope, "0.90", action_id=action)

    assert again == first
    assert entries(sessions, scope) == 1


def test_committing_records_what_was_spent_and_gives_back_the_hold(action, sessions, scope):
    reserve(sessions, scope, "0.20", action_id=action)

    committed = commit(sessions, scope, action, "0.18")

    assert committed.settlement is LedgerEntryKind.COMMIT
    assert committed.settled_usd == Decimal("0.18")
    month = balance(sessions, scope)
    assert (month.spent_usd, month.reserved_usd) == (Decimal("0.18"), Decimal(0))
    assert month.remaining_usd == Decimal("0.82")


def test_a_retried_commit_keeps_the_amount_first_committed(action, sessions, scope):
    reserve(sessions, scope, "0.20", action_id=action)
    commit(sessions, scope, action, "0.18")

    again = commit(sessions, scope, action, "0.30")

    assert again.settled_usd == Decimal("0.18")
    assert entries(sessions, scope) == 2


def test_releasing_gives_back_the_hold_and_spends_nothing(action, sessions, scope):
    reserve(sessions, scope, "0.50", action_id=action)

    released = release(sessions, scope, action)
    again = release(sessions, scope, action)

    assert released == again
    assert released.settlement is LedgerEntryKind.RELEASE
    month = balance(sessions, scope)
    assert (month.spent_usd, month.reserved_usd) == (Decimal(0), Decimal(0))


def test_a_released_spend_is_not_committed(action, sessions, scope):
    reserve(sessions, scope, "0.50", action_id=action)
    release(sessions, scope, action)

    with pytest.raises(SpendAlreadySettled, match="released"):
        commit(sessions, scope, action, "0.50")


def test_a_committed_spend_is_not_released(action, sessions, scope):
    reserve(sessions, scope, "0.20", action_id=action)
    commit(sessions, scope, action, "0.20")

    with pytest.raises(SpendAlreadySettled, match="committed"):
        release(sessions, scope, action)


def test_settling_an_action_without_a_reservation_is_refused(sessions, scope):
    with pytest.raises(ReservationNotFound):
        commit(sessions, scope, "never-reserved", "0.10")


def test_a_reservation_of_another_workspace_is_missing(action, sessions, scope):
    reserve(sessions, scope, "0.20", action_id=action)
    other = new_scope(sessions)

    with pytest.raises(ReservationNotFound):
        release(sessions, other, action)
    with pytest.raises(ReservationNotFound):
        reserve(sessions, other, "0.20", action_id=action)


def test_a_project_of_another_workspace_is_not_found(sessions, scope):
    other = new_scope(sessions)

    with pytest.raises(ProjectNotFound):
        reserve(sessions, Scope(scope.workspace_id, other.project_id), "0.20")


def test_another_workspaces_spending_does_not_count(sessions, scope):
    reserve(sessions, new_scope(sessions), "0.90")

    assert reserve(sessions, scope, "0.90").decision is SpendDecision.ASK_A_PERSON


def test_a_spend_counts_in_the_month_it_was_reserved_in(sessions, scope):
    with transaction(sessions) as session:
        start = month_balance(session, workspace_id=scope.workspace_id).month_start
        # Reserved a second before the month began, then committed in it.
        session.add(
            ledger_entry(
                scope,
                LedgerEntryKind.RESERVE,
                action_id="last-month",
                amount_usd=Decimal("0.90"),
                created_at=start - timedelta(seconds=1),
            )
        )
    commit(sessions, scope, "last-month", "0.90")

    month = balance(sessions, scope)
    assert (month.spent_usd, month.reserved_usd) == (Decimal(0), Decimal(0))
    assert reserve(sessions, scope, "0.90").decision is SpendDecision.ASK_A_PERSON


def test_two_reservations_that_together_pass_the_cap_hold_only_one(sessions, scope):
    start = threading.Barrier(2)
    decisions: list[SpendDecision] = []

    def reserve_at_once() -> None:
        start.wait()
        decisions.append(reserve(sessions, scope, "0.60").decision)

    racers = [threading.Thread(target=reserve_at_once) for _ in range(2)]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join(timeout=30)

    assert sorted(decisions) == sorted([SpendDecision.ASK_A_PERSON, SpendDecision.REFUSE])
    assert balance(sessions, scope).reserved_usd == Decimal("0.60")


def test_the_ledger_refuses_a_change(action, sessions, scope):
    reserve(sessions, scope, "0.20", action_id=action)

    with pytest.raises(DatabaseError, match="cannot be changed or deleted"):
        with transaction(sessions) as session:
            session.execute(
                update(BudgetLedgerEntryRecord)
                .where(BudgetLedgerEntryRecord.action_id == action)
                .values(amount_usd=0)
            )


def test_the_ledger_refuses_a_deletion(action, sessions, scope):
    reserve(sessions, scope, "0.20", action_id=action)

    with pytest.raises(DatabaseError, match="cannot be changed or deleted"):
        with transaction(sessions) as session:
            session.execute(
                delete(BudgetLedgerEntryRecord).where(BudgetLedgerEntryRecord.action_id == action)
            )


def test_the_ledger_refuses_a_settlement_without_a_reservation(session, scope, action):
    session.add(ledger_entry(scope, LedgerEntryKind.COMMIT, action_id=action))

    with pytest.raises(IntegrityError, match="settles only a reservation"):
        session.flush()


def test_the_ledger_holds_one_reservation_per_action(session, scope, action):
    session.add(ledger_entry(scope, LedgerEntryKind.RESERVE, action_id=action))
    session.add(ledger_entry(scope, LedgerEntryKind.RESERVE, action_id=action))

    with pytest.raises(IntegrityError, match="uq_budget_ledger_entry_reserve"):
        session.flush()


def test_the_ledger_holds_one_settlement_per_action(session, scope, action):
    session.add(ledger_entry(scope, LedgerEntryKind.RESERVE, action_id=action))
    session.flush()
    session.add(ledger_entry(scope, LedgerEntryKind.COMMIT, decision=None, action_id=action))
    session.add(ledger_entry(scope, LedgerEntryKind.RELEASE, amount_usd=0, action_id=action))

    with pytest.raises(IntegrityError, match="uq_budget_ledger_entry_settlement"):
        session.flush()


@pytest.mark.parametrize(
    ("kind", "columns", "constraint"),
    [
        (LedgerEntryKind.RESERVE, {"amount_usd": Decimal(-1)}, "ck_budget_ledger_entry_amount_usd"),
        (LedgerEntryKind.RESERVE, {"decision": None}, "ck_budget_ledger_entry_reserve_decision"),
        (
            LedgerEntryKind.RESERVE,
            {"decision": SpendDecision.REFUSE},
            "ck_budget_ledger_entry_decision_held",
        ),
    ],
)
def test_the_database_checks_a_reservation(session, scope, action, kind, columns, constraint):
    session.add(ledger_entry(scope, kind, **columns, action_id=action))

    with pytest.raises(IntegrityError, match=constraint):
        session.flush()


def test_the_database_records_a_release_as_zero(session, scope, action):
    session.add(ledger_entry(scope, LedgerEntryKind.RESERVE, action_id=action))
    session.flush()
    session.add(ledger_entry(scope, LedgerEntryKind.RELEASE, action_id=action))

    with pytest.raises(IntegrityError, match="ck_budget_ledger_entry_release_amount"):
        session.flush()
