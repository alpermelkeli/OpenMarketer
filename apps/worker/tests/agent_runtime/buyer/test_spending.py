"""The creative budget on a real Temporal server: an action that spends is weighed before it runs.

The buyer asks for one image. Its estimate is reserved in the ledger, whose decision is the
gate's verdict: a small one runs at once, a larger one waits for a person, one past the month's
cap is refused. Once the action ran, its spend is committed; when it did not, released. The
tests decide proposals the way the API does and read the ledger and the history back.
"""

import asyncio
import uuid
from decimal import Decimal

import pytest
from scripted_history import exchanges
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError

from buyer.definitions import KIND, STUDIO_REFUSED
from openmarketer_core.agent_runtime.gate import ProposalStatus
from openmarketer_core.budget.caps import SpendDecision
from openmarketer_core.db.models import LedgerEntryKind
from openmarketer_core.db.session import transaction
from openmarketer_core.workspace_settings.creative_budget import CreativeBudgetChoice
from openmarketer_core.workspace_settings.creative_budget_settings import set_creative_budget
from openmarketer_worker.agent_runtime.vocabulary import (
    RESERVE_SPEND,
    SETTLE_SPEND,
    SpendInput,
    SpendReserved,
)

PERSON = uuid.uuid4()


async def result_of(handle) -> dict:
    return await asyncio.wait_for(handle.result(), timeout=30)


async def test_spend_within_what_one_action_may_spend_runs_without_a_person(serving, studio):
    async with serving() as desk:
        _, handle = await desk.start()

        assert await result_of(handle) == {"outcome": "made image-1"}
        assert desk.proposals() == ()
    [(size, key)] = studio.made
    assert size == "small"
    held = desk.reservation(key)
    assert held is not None
    assert held.decision is SpendDecision.RUN_AUTOMATICALLY


async def test_spend_that_ran_is_committed_at_what_the_handler_reported(serving, studio):
    studio.charges = "0.08"
    async with serving() as desk:
        _, handle = await desk.start()
        await result_of(handle)

    [(_, key)] = studio.made
    held = desk.reservation(key)
    assert held is not None
    assert (held.settlement, held.settled_usd) == (LedgerEntryKind.COMMIT, Decimal("0.08"))


async def test_spend_whose_handler_reported_nothing_is_committed_at_its_estimate(serving, studio):
    async with serving() as desk:
        _, handle = await desk.start()
        await result_of(handle)

    [(_, key)] = studio.made
    held = desk.reservation(key)
    assert held is not None
    assert (held.settlement, held.settled_usd) == (LedgerEntryKind.COMMIT, Decimal("0.10"))


async def test_larger_spend_waits_for_a_person_with_its_estimate_shown(serving, studio):
    async with serving() as desk:
        run_id = uuid.uuid4()
        studio.sizes[run_id] = "large"
        _, handle = await desk.start(run_id)
        proposal = await desk.pending_proposal()
        assert studio.made == []

        assert proposal.effect.value == "spends"
        assert proposal.estimate_usd == Decimal("0.50")
        [key] = set(studio.estimated)
        held = desk.reservation(key)
        assert held is not None
        assert (held.decision, held.settlement) == (SpendDecision.ASK_A_PERSON, None)
        await desk.decide(handle, proposal, approved=False)
        await result_of(handle)


async def test_approved_spend_runs_and_is_committed(serving, studio):
    async with serving() as desk:
        run_id = uuid.uuid4()
        studio.sizes[run_id] = "large"
        _, handle = await desk.start(run_id)
        proposal = await desk.pending_proposal()

        await desk.decide(handle, proposal, approved=True)

        assert await result_of(handle) == {"outcome": "made image-1"}
    [key] = set(studio.estimated)
    assert studio.made == [("large", key)]
    held = desk.reservation(key)
    assert held is not None
    assert (held.settlement, held.settled_usd) == (LedgerEntryKind.COMMIT, Decimal("0.50"))


async def test_rejected_spend_does_not_run_and_is_released(serving, studio):
    async with serving() as desk:
        run_id = uuid.uuid4()
        studio.sizes[run_id] = "large"
        _, handle = await desk.start(run_id)
        proposal = await desk.pending_proposal()

        await desk.decide(handle, proposal, approved=False)

        assert await result_of(handle) == {"outcome": "not made: a person rejected the action"}
    assert studio.made == []
    [key] = set(studio.estimated)
    held = desk.reservation(key)
    assert held is not None
    assert (held.settlement, held.settled_usd) == (LedgerEntryKind.RELEASE, Decimal(0))


async def test_spend_past_the_months_cap_is_refused_and_nothing_is_reserved(serving, studio):
    async with serving() as desk:
        run_id = uuid.uuid4()
        studio.sizes[run_id] = "huge"
        _, handle = await desk.start(run_id)

        assert await result_of(handle) == {
            "outcome": "not made: the creative budget for this month would be exceeded"
        }
        assert desk.proposals() == ()
        assert desk.ledger_entries() == 0
    assert studio.made == []


async def test_spend_whose_handler_failed_for_good_is_released(serving, studio):
    studio.failures = [
        ApplicationError(
            "the studio cannot make this image", type=STUDIO_REFUSED, non_retryable=True
        )
    ]
    async with serving() as desk:
        _, handle = await desk.start()

        assert await result_of(handle) == {"outcome": "not made: the studio cannot make this image"}
    [key] = set(studio.estimated)
    held = desk.reservation(key)
    assert held is not None
    assert held.settlement is LedgerEntryKind.RELEASE


async def test_spend_whose_handler_stopped_on_an_error_nobody_worded_is_committed_at_its_estimate(
    serving, studio
):
    # The service may have charged for work the runtime did not see finish.
    studio.failures = [RuntimeError("the connection dropped"), RuntimeError("again")]
    async with serving() as desk:
        _, handle = await desk.start()

        assert await result_of(handle) == {"outcome": "not made: the action stopped unexpectedly"}
    [key] = set(studio.estimated)
    held = desk.reservation(key)
    assert held is not None
    assert (held.settlement, held.settled_usd) == (LedgerEntryKind.COMMIT, Decimal("0.10"))


async def test_cancelled_run_releases_the_spend_it_waited_for(serving, studio):
    async with serving() as desk:
        run_id = uuid.uuid4()
        studio.sizes[run_id] = "large"
        _, handle = await desk.start(run_id)
        await desk.pending_proposal()

        await handle.cancel()
        with pytest.raises(WorkflowFailureError):
            await result_of(handle)

        assert [p.status for p in desk.proposals()] == [ProposalStatus.EXPIRED]
    [key] = set(studio.estimated)
    held = desk.reservation(key)
    assert held is not None
    assert held.settlement is LedgerEntryKind.RELEASE
    assert studio.made == []


async def test_reservation_tried_again_finds_the_one_made_before(serving, studio):
    answered: list[SpendReserved] = []

    def lose_the_first_answer(activities: list) -> list:
        real = next(a for a in activities if a.__name__ == RESERVE_SPEND)

        @activity.defn(name=RESERVE_SPEND)
        def reserve_spend(spend: SpendInput) -> SpendReserved:
            reserved = real(spend)
            answered.append(reserved)
            if len(answered) == 1:
                # The reservation is written, and the attempt fails before it answers.
                raise ApplicationError("the worker lost its answer")
            return reserved

        return [reserve_spend, *(a for a in activities if a is not real)]

    async with serving(replace=lose_the_first_answer) as desk:
        _, handle = await desk.start()

        assert await result_of(handle) == {"outcome": "made image-1"}
        assert desk.ledger_entries() == 2  # one reservation, and its commit
    assert answered[0] == answered[1]
    assert len(studio.estimated) == 2


async def test_caps_are_the_workspaces_stored_choice_when_the_environment_sets_none(
    serving, studio, sessions, scope
):
    with transaction(sessions) as session:
        set_creative_budget(
            session,
            {},
            workspace_id=scope.workspace_id,
            choice=CreativeBudgetChoice(monthly_usd=Decimal("0.05")),
            set_by=PERSON,
        )
    async with serving(environ={}) as desk:
        _, handle = await desk.start()

        assert await result_of(handle) == {
            "outcome": "not made: the creative budget for this month would be exceeded"
        }
    assert studio.made == []


async def test_history_holds_identifiers_and_amounts_of_a_spend_only(serving, studio):
    studio.charges = "0.08"
    async with serving() as desk:
        run, handle = await desk.start()
        await result_of(handle)
        history = await handle.fetch_history()

    [(_, key)] = studio.made
    scope = {"workspace_id": str(run.workspace_id), "project_id": str(run.project_id)}
    budget = [e for e in exchanges(history) if e.activity in (RESERVE_SPEND, SETTLE_SPEND)]
    assert [(e.activity, e.given, e.returned) for e in budget] == [
        (
            RESERVE_SPEND,
            {
                "scope": scope,
                "run": {"kind": KIND, "run_id": str(run.run_id)},
                "request": {
                    "action_id": key,
                    "name": "generate_image",
                    "payload": {"size": "small"},
                },
            },
            {"decision": "run_automatically", "estimate_usd": "0.100000"},
        ),
        (
            SETTLE_SPEND,
            {
                "scope": scope,
                "action_id": key,
                "spent": True,
                "estimate_usd": "0.100000",
                "reported_usd": "0.08",
            },
            "0.080000",
        ),
    ]
