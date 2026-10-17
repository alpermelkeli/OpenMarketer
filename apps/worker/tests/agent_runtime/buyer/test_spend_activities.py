"""The budget's activities as plain functions, and the check ``run_action`` makes of a spend.

They need PostgreSQL (see the root ``conftest.py``); no workflow runs.
"""

import uuid

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from buyer.definitions import KIND
from buyer.rig import Desk
from buyer.wiring import Studio, wire
from openmarketer_core.agent_runtime.actions import ActionRequest
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.db.models import LedgerEntryKind
from openmarketer_worker.agent_runtime.vocabulary import (
    ACTION_NOT_APPROVED,
    RESERVE_SPEND,
    RUN_ACTION,
    SETTLE_SPEND,
    SPEND_NOT_RESERVED,
    SPEND_NOT_SETTLED,
    UNKNOWN_ACTION,
    SpendInput,
    SpendReserved,
    SpendSettlement,
)


@pytest.fixture
def registration(studio, checkpoints, sessions):
    return wire(studio, checkpoints, sessions)


@pytest.fixture
def desk(sessions, scope) -> Desk:
    return Desk(None, "", sessions, scope)  # pyright: ignore[reportArgumentType]


def activity_named(registration, name: str):
    return next(a for a in registration.activities if a.__name__ == name)


def spend_of(scope, *, size: str = "small", name: str = "generate_image", kind: str = KIND):
    run = GraphRunRef(kind=kind, run_id=uuid.uuid4())
    request = ActionRequest(f"{kind}:{run.run_id}:{uuid.uuid4().hex}", name, {"size": size})
    return SpendInput(scope=scope, run=run, request=request)


def reserve(registration, spend: SpendInput) -> SpendReserved:
    return ActivityEnvironment().run(activity_named(registration, RESERVE_SPEND), spend)


def settle(registration, settlement: SpendSettlement) -> str:
    return ActivityEnvironment().run(activity_named(registration, SETTLE_SPEND), settlement)


def test_reserving_the_same_action_twice_holds_it_once(registration, scope, desk):
    spend = spend_of(scope)

    first, again = reserve(registration, spend), reserve(registration, spend)

    assert first == again == SpendReserved(decision="run_automatically", estimate_usd="0.100000")
    assert desk.ledger_entries() == 1


def test_spend_of_an_action_that_does_not_spend_is_refused(registration, scope, desk):
    with pytest.raises(ApplicationError) as refused:
        reserve(registration, spend_of(scope, name="draw_a_cheque"))

    assert (refused.value.type, refused.value.non_retryable) == (UNKNOWN_ACTION, True)
    assert desk.ledger_entries() == 0


def test_spend_of_a_run_of_another_kind_is_refused(registration, scope, desk):
    with pytest.raises(ApplicationError) as refused:
        reserve(registration, spend_of(scope, kind="test-elsewhere"))

    assert (refused.value.type, refused.value.non_retryable) == (SPEND_NOT_RESERVED, True)
    assert desk.ledger_entries() == 0


def test_spend_for_a_project_the_workspace_does_not_have_is_refused(registration, scope):
    elsewhere = type(scope)(workspace_id=scope.workspace_id, project_id=uuid.uuid4())

    with pytest.raises(ApplicationError) as refused:
        reserve(registration, spend_of(elsewhere))

    assert (refused.value.type, refused.value.non_retryable) == (SPEND_NOT_RESERVED, True)


def test_cap_the_environment_sets_wrong_refuses_the_spend_and_names_the_variable(
    studio, checkpoints, sessions, scope
):
    registration = wire(studio, checkpoints, sessions, {"MEDIA_MONTHLY_CAP_USD": "lots"})

    with pytest.raises(ApplicationError) as refused:
        reserve(registration, spend_of(scope))

    assert refused.value.type == SPEND_NOT_RESERVED
    assert "MEDIA_MONTHLY_CAP_USD" in refused.value.message


def test_settlement_with_a_reported_cost_that_is_not_an_amount_commits_the_estimate(
    registration, scope, desk
):
    spend = spend_of(scope)
    reserved = reserve(registration, spend)

    spent = settle(
        registration,
        SpendSettlement(
            scope=scope,
            action_id=spend.request.action_id,
            spent=True,
            estimate_usd=reserved.estimate_usd,
            reported_usd="a lot",
        ),
    )

    assert spent == "0.100000"
    held = desk.reservation(spend.request.action_id)
    assert held is not None
    assert held.settlement is LedgerEntryKind.COMMIT


def test_spend_that_was_released_is_not_committed(registration, scope):
    spend = spend_of(scope)
    reserved = reserve(registration, spend)
    settlement = SpendSettlement(
        scope=scope, action_id=spend.request.action_id, spent=False, estimate_usd="0.1"
    )
    settle(registration, settlement)

    with pytest.raises(ApplicationError) as refused:
        settle(
            registration,
            SpendSettlement(
                scope=scope,
                action_id=spend.request.action_id,
                spent=True,
                estimate_usd=reserved.estimate_usd,
            ),
        )

    assert (refused.value.type, refused.value.non_retryable) == (SPEND_NOT_SETTLED, True)


async def test_spend_that_holds_no_reservation_is_refused_by_run_action_itself(
    registration, studio: Studio
):
    request = ActionRequest(f"{KIND}:{uuid.uuid4()}:1", "generate_image", {"size": "small"})

    with pytest.raises(ApplicationError) as refused:
        await ActivityEnvironment().run(activity_named(registration, RUN_ACTION), request)

    assert (refused.value.type, refused.value.non_retryable) == (ACTION_NOT_APPROVED, True)
    assert studio.made == []


async def test_spend_that_waits_for_a_person_is_refused_by_run_action_until_approved(
    registration, scope, studio: Studio
):
    spend = spend_of(scope, size="large")
    assert reserve(registration, spend).decision == "ask_a_person"

    with pytest.raises(ApplicationError) as refused:
        await ActivityEnvironment().run(activity_named(registration, RUN_ACTION), spend.request)

    assert refused.value.type == ACTION_NOT_APPROVED
    assert studio.made == []


async def test_spend_the_budget_let_through_runs(registration, scope, studio: Studio):
    spend = spend_of(scope)
    reserve(registration, spend)

    outcome = await ActivityEnvironment().run(
        activity_named(registration, RUN_ACTION), spend.request
    )

    assert outcome.succeeded
    assert studio.made == [("small", spend.request.action_id)]
