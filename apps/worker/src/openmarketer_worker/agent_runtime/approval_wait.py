"""``wait_for_a_person``: how a run holds an action the gate stopped until a person decides.

The driver calls it for an action whose verdict is ``ASK_A_PERSON`` (core's ``gate.py``): one
that publishes, or one that spends more than the creative budget lets run on its own, whose
estimate the proposal shows. It stores the action as a proposal (the activity
``propose_action``), then waits, with no activity running, until one of three things happens:

- the API signals that a person decided (``PROPOSAL_DECIDED_SIGNAL``, heard by the agent's
  workflow into ``DecidedProposals``);
- ``APPROVAL_RECHECK_SECONDS`` pass, in case a signal never came;
- the proposal's deadline passes.

Each time it reads the proposal back (``settle_proposal``), which expires it once the deadline
passed, and returns its status as soon as it is no longer pending. The decision is read from the
database and never taken from the signal: anybody who reaches the workflow server can send a
signal, and a signal says only that there is something to read. A run that is cancelled while it
stores the proposal or waits expires the proposal before the cancellation goes on, so nobody
approves an action whose run is gone.

This is workflow code, loaded by the sandbox: what it waits on is the workflow's own timer and
state, and every read or write is an activity.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from openmarketer_core.agent_runtime.actions import ActionPolicy, ActionRequest
    from openmarketer_core.agent_runtime.gate import ProposalStatus, RunScope
    from openmarketer_core.agent_runtime.steps import GraphRunRef
    from openmarketer_worker.agent_runtime.vocabulary import (
        APPROVAL_RECHECK_SECONDS,
        PROPOSAL_REFUSED,
        PROPOSAL_WRITE_LIMITS,
        PROPOSE_ACTION,
        SETTLE_PROPOSAL,
        ProposalHeld,
        ProposalInput,
        SettleInput,
    )
    from openmarketer_worker.agent_runtime.workflow_support import database_write, until_it_ends


class DecidedProposals:
    """The proposals of a run that a person decided on, as far as the workflow has heard."""

    def __init__(self) -> None:
        self._heard: set[uuid.UUID] = set()

    def hear(self, proposal_id: uuid.UUID) -> None:
        self._heard.add(proposal_id)

    def heard_of(self, proposal_id: uuid.UUID) -> bool:
        return proposal_id in self._heard

    def forget(self, proposal_id: uuid.UUID) -> None:
        self._heard.discard(proposal_id)


async def wait_for_a_person(
    run: GraphRunRef,
    scope: RunScope,
    request: ActionRequest,
    limits: ActionPolicy,
    decided: DecidedProposals,
    *,
    estimate_usd: str | None = None,
) -> ProposalStatus:
    """Store ``request`` as a proposal and wait until it is approved, rejected or expired.

    ``estimate_usd`` is what an action that spends is estimated to cost, stored with the
    proposal for the person who decides.
    """
    held, cancelled = await _proposed(run, scope, request, limits, estimate_usd)
    try:
        if cancelled:
            raise asyncio.CancelledError
        deadline = workflow.now() + timedelta(seconds=held.seconds_left)
        while True:
            left = (deadline - workflow.now()).total_seconds()
            if left > 0:
                await _hear_or_time_out(
                    decided, held.proposal_id, min(left, APPROVAL_RECHECK_SECONDS)
                )
            status = await _settle(scope, request, deadline_passed=workflow.now() >= deadline)
            if status != ProposalStatus.PENDING:
                return status
            decided.forget(held.proposal_id)
    except asyncio.CancelledError:
        await until_it_ends(_settle(scope, request, deadline_passed=True))
        raise


async def _proposed(
    run: GraphRunRef,
    scope: RunScope,
    request: ActionRequest,
    limits: ActionPolicy,
    estimate_usd: str | None,
) -> tuple[ProposalHeld, bool]:
    """The proposal, stored to its end even if the workflow is cancelled meanwhile, and whether
    it was.

    A cancellation that arrived while the row was being written would otherwise leave the
    proposal stored and pending, with no run waiting for it to expire it. Like
    ``workflow_support.until_it_ends``, the write runs as a task of its own that a cancellation
    does not reach; unlike it, the cancellation is reported instead of dropped.
    """
    storing = asyncio.ensure_future(
        database_write(
            PROPOSE_ACTION,
            ProposalInput(
                scope=scope,
                run=run,
                workflow_id=workflow.info().workflow_id,
                request=request,
                effect=limits.effect.value,
                wait_seconds=limits.approval_wait_seconds,
                estimate_usd=estimate_usd,
            ),
            PROPOSAL_WRITE_LIMITS,
            not_retried=[PROPOSAL_REFUSED],
            result_type=ProposalHeld,
        )
    )
    cancelled = False
    while not storing.done():
        try:
            await workflow.wait([storing])
        except asyncio.CancelledError:
            cancelled = True
    return storing.result(), cancelled


async def _hear_or_time_out(
    decided: DecidedProposals, proposal_id: uuid.UUID, timeout_seconds: float
) -> None:
    try:
        await workflow.wait_condition(
            lambda: decided.heard_of(proposal_id), timeout=timeout_seconds
        )
    except TimeoutError:
        return


async def _settle(
    scope: RunScope, request: ActionRequest, *, deadline_passed: bool
) -> ProposalStatus:
    status = await database_write(
        SETTLE_PROPOSAL,
        SettleInput(scope=scope, action_id=request.action_id, deadline_passed=deadline_passed),
        PROPOSAL_WRITE_LIMITS,
        not_retried=[PROPOSAL_REFUSED],
        result_type=str,
    )
    return ProposalStatus(status)
