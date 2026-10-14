"""The policy gate on a real Temporal server: an action that publishes waits for a person.

The announcer asks to publish once. Its run stores a proposal and waits; a test decides it the
way the API does (in the database, then a signal) and reads what the graph was told.
"""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from dsl.rig import cancel_and_let_it_be_seen
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

from announcer.definitions import KIND
from announcer.rig import Desk
from announcer.wiring import wire
from openmarketer_core.agent_runtime.actions import ActionRequest
from openmarketer_core.agent_runtime.gate import PROPOSAL_DECIDED_SIGNAL, ProposalStatus
from openmarketer_worker.agent_runtime.vocabulary import (
    ACTION_NOT_APPROVED,
    PROPOSE_ACTION,
    RUN_ACTION,
    ProposalHeld,
    ProposalInput,
)


async def result_of(handle) -> dict:
    return await asyncio.wait_for(handle.result(), timeout=30)


async def test_action_that_publishes_runs_only_once_a_person_approved_it(serving, board):
    async with serving() as desk:
        run, handle = await desk.start()
        proposal = await desk.pending_proposal()
        assert board.published == []

        await desk.decide(handle, proposal, approved=True)

        assert await result_of(handle) == {"outcome": "published as post-1"}
    [(text, key)] = board.published
    assert text == "v2 is out"
    assert key.startswith(f"{KIND}:{run.run_id}:")  # the action's idempotency key


async def test_proposal_holds_what_the_agent_asked_for_and_whose_run_it_is(serving, board):
    async with serving() as desk:
        run, handle = await desk.start()
        proposal = await desk.pending_proposal()
        await desk.decide(handle, proposal, approved=False)
        await result_of(handle)

    assert (proposal.action_name, proposal.payload) == ("announce", {"text": "v2 is out"})
    assert (proposal.run_kind, proposal.run_id) == (KIND, run.run_id)
    assert proposal.workflow_id == f"announcer-{run.run_id}"
    assert proposal.effect.value == "publishes"


async def test_rejected_action_does_not_run_and_the_graph_is_told_why(serving, board):
    async with serving() as desk:
        _, handle = await desk.start()
        await desk.decide(handle, await desk.pending_proposal(), approved=False)

        assert await result_of(handle) == {"outcome": "not published: a person rejected the action"}
    assert board.published == []


async def test_action_nobody_decided_on_expires_at_its_deadline(serving, board):
    async with serving() as desk:
        run_id = uuid.uuid4()
        board.soon.add(run_id)
        _, handle = await desk.start(run_id)
        outcome = await result_of(handle)

        assert outcome == {
            "outcome": "not published: nobody approved the action before its deadline"
        }
        assert [p.status for p in desk.proposals()] == [ProposalStatus.EXPIRED]
    assert board.published == []


async def test_signal_alone_approves_nothing(serving, board):
    async with serving() as desk:
        _, handle = await desk.start()
        proposal = await desk.pending_proposal()

        await handle.signal(PROPOSAL_DECIDED_SIGNAL, proposal.id)
        await asyncio.sleep(1)
        assert board.published == []
        assert [p.status for p in desk.proposals()] == [ProposalStatus.PENDING]

        await desk.decide(handle, proposal, approved=True)
        assert await result_of(handle) == {"outcome": "published as post-1"}


async def test_cancelled_run_expires_the_proposal_it_waited_for(serving, board):
    async with serving() as desk:
        _, handle = await desk.start()
        await desk.pending_proposal()

        await handle.cancel()
        with pytest.raises(WorkflowFailureError):
            await result_of(handle)

        assert [p.status for p in desk.proposals()] == [ProposalStatus.EXPIRED]
    assert board.published == []


async def test_run_cancelled_while_its_proposal_is_stored_expires_it(
    temporal, task_queue, checkpoints, sessions, board, scope
):
    # The proposal is written while the workflow waits for the activity to answer; the
    # cancellation arrives in between, and must not leave the proposal pending.
    registration = wire(board, checkpoints, sessions)
    real = next(a for a in registration.activities if a.__name__ == PROPOSE_ACTION)
    arrived, release = asyncio.Event(), asyncio.Event()

    @activity.defn(name=PROPOSE_ACTION)
    async def held_proposing(proposal: ProposalInput) -> ProposalHeld:
        held = await asyncio.to_thread(real, proposal)
        arrived.set()
        await asyncio.wait_for(release.wait(), timeout=30)
        return held

    activities = [held_proposing, *(a for a in registration.activities if a is not real)]
    async with Worker(
        temporal,
        task_queue=task_queue,
        workflows=registration.workflows,
        activities=activities,
        activity_executor=ThreadPoolExecutor(max_workers=4),
    ):
        desk = Desk(temporal, task_queue, sessions, scope)
        _, handle = await desk.start()
        await asyncio.wait_for(arrived.wait(), timeout=20)
        await cancel_and_let_it_be_seen(handle)
        release.set()
        with pytest.raises(WorkflowFailureError):
            await result_of(handle)

        assert [p.status for p in desk.proposals()] == [ProposalStatus.EXPIRED]
    assert board.published == []


async def test_held_action_that_nobody_approved_is_refused_by_the_activity_itself(
    board, checkpoints, sessions
):
    registration = wire(board, checkpoints, sessions)
    run_action = next(a for a in registration.activities if a.__name__ == RUN_ACTION)
    request = ActionRequest(f"{KIND}:r:1", "announce", {"text": "v2 is out"})

    with pytest.raises(ApplicationError) as refused:
        await ActivityEnvironment().run(run_action, request)

    assert refused.value.type == ACTION_NOT_APPROVED
    assert refused.value.non_retryable
    assert board.published == []
