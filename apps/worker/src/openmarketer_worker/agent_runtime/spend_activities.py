"""The creative budget's activities: estimating and reserving a spend, then settling it.

``reserve_spend`` estimates what an action that spends will cost, with the estimate its agent
declared (``AgentWorker.estimate``), resolves the caps of the run's workspace for this attempt
(core's ``creative_caps``: the environment wins, then the workspace's stored choice, then the
default) and reserves the estimate in the ledger (core's ``budget/ledger.py``), whose decision
is the gate's verdict: run, ask a person, or refuse. ``settle_spend`` commits what the action
spent once it ran, or releases the hold when it did not. ``driver.py`` calls both.
``is_cleared`` is not an activity: it is how ``run_action`` checks, before it runs the handler
of an action that spends, that the ledger holds a reservation for that very action and that a
person approved it when the ledger asked for one, so that a workflow that skipped the gate
still spends nothing the budget did not let through.

They hold no rule: the decision is core's ``decide_spend``, and the ledger's use cases are
idempotent by the action's id, so a retried reservation finds the one made before, with its
decision and its amount, and a retried settlement changes nothing. A refused spend is written
nowhere, so a retry decides it again. A spend that cannot be estimated or reserved for the
run's project fails for good (``SPEND_NOT_RESERVED``), and one that cannot be settled
(``SPEND_NOT_SETTLED``), worded with the action's id or the rule at fault and never a value of
the payload; any other failure is worded by ``worded_failures``. What they answer is the
decision's value and amounts as text: nothing but identifiers and amounts enters the history.

This module is for the worker process: the workflow sandbox never loads it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from openmarketer_core.agent_runtime.actions import (
    ActionCatalog,
    ActionRequest,
    InvalidActionPayload,
    UnknownAction,
)
from openmarketer_core.agent_runtime.gate import ProposalStatus
from openmarketer_core.agent_runtime.model_input import shown
from openmarketer_core.budget.caps import InvalidAmount, SpendDecision, usd
from openmarketer_core.budget.ledger import (
    ReservationNotFound,
    SpendAlreadySettled,
    commit_spend,
    release_spend,
    reserve_spend,
)
from openmarketer_core.db.budget_ledger import held_decision_of_action
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.proposals import proposal_status_of_action
from openmarketer_core.db.session import transaction
from openmarketer_core.workspace_settings.creative_budget_settings import (
    CreativeBudgetEnvironmentInvalid,
    StoredCreativeBudgetInvalid,
    creative_caps,
)
from openmarketer_worker.agent_runtime.activity_support import worded_failures
from openmarketer_worker.agent_runtime.routes import SpendContext, SpendEstimate
from openmarketer_worker.agent_runtime.vocabulary import (
    ACTION_TEXTS,
    INVALID_ACTION_PAYLOAD,
    RESERVE_SPEND,
    SETTLE_SPEND,
    SPEND_NOT_RESERVED,
    SPEND_NOT_SETTLED,
    UNKNOWN_ACTION,
    SpendInput,
    SpendReserved,
    SpendSettlement,
)

logger = logging.getLogger(__name__)


class SpendActivities:
    """The budget's activities for the actions of one agent that spend."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        environ: Mapping[str, str],
        *,
        kind: str,
        catalog: ActionCatalog,
        estimates: Mapping[str, SpendEstimate],
    ) -> None:
        """``catalog`` holds the agent's actions that spend, and ``estimates`` the estimate of
        each, by name. ``environ`` is the process's environment, which a cap set there wins
        from: it is read at each attempt, as the workspace's stored choice is."""
        self._sessions = sessions
        self._environ = environ
        self._kind = kind
        self._catalog = catalog
        self._estimates = estimates

    @activity.defn(name=RESERVE_SPEND)
    def reserve_spend(self, spend: SpendInput) -> SpendReserved:
        """Estimate the action's cost and reserve it under the workspace's caps, or find the
        reservation made for it before; the ledger's decision and the amount it decided on."""
        request = spend.request
        subject = f"spend of action {request.action_id}"
        refusals = _refusals(subject, as_type=SPEND_NOT_RESERVED)
        with worded_failures(ACTION_TEXTS, subject=subject), refusals:
            if spend.run.kind != self._kind:
                # Quoted and short: the kind is whatever the workflow was started with.
                raise _NotReserved(
                    f"a run of kind {shown(spend.run.kind)} cannot spend for agent {self._kind!r}"
                )
            payload = self._validated(request, subject)
            estimate = usd(
                self._estimates[request.name](
                    payload, SpendContext(action_id=request.action_id, scope=spend.scope)
                ),
                "the estimate",
            )
            with transaction(self._sessions) as session:
                caps = creative_caps(session, self._environ, workspace_id=spend.scope.workspace_id)
            reservation = reserve_spend(
                self._sessions,
                workspace_id=spend.scope.workspace_id,
                project_id=spend.scope.project_id,
                run_kind=self._kind,
                run_id=spend.run.run_id,
                action_id=request.action_id,
                estimate_usd=estimate,
                caps=caps,
            )
            return SpendReserved(
                decision=reservation.decision.value, estimate_usd=str(reservation.amount_usd)
            )

    @activity.defn(name=SETTLE_SPEND)
    def settle_spend(self, settlement: SpendSettlement) -> str:
        """Commit what the action spent, or release its hold; what was spent, as text."""
        subject = f"spend of action {settlement.action_id}"
        refusals = _refusals(subject, as_type=SPEND_NOT_SETTLED)
        with worded_failures(ACTION_TEXTS, subject=subject), refusals:
            if not settlement.spent:
                settled = release_spend(
                    self._sessions,
                    workspace_id=settlement.scope.workspace_id,
                    run_kind=self._kind,
                    action_id=settlement.action_id,
                )
            else:
                settled = commit_spend(
                    self._sessions,
                    workspace_id=settlement.scope.workspace_id,
                    run_kind=self._kind,
                    action_id=settlement.action_id,
                    actual_usd=_spent(settlement, subject),
                )
            return str(settled.settled_usd)

    def is_cleared(self, action_id: str) -> bool:
        """Whether the budget let the action's spend through: held, and approved if it asked
        for a person. It blocks."""
        with transaction(self._sessions) as session:
            decision = held_decision_of_action(session, run_kind=self._kind, action_id=action_id)
            if decision is SpendDecision.RUN_AUTOMATICALLY:
                return True
            if decision is SpendDecision.ASK_A_PERSON:
                return proposal_status_of_action(session, action_id) is ProposalStatus.APPROVED
            return False

    def _validated(self, request: ActionRequest, subject: str) -> BaseModel:
        try:
            return self._catalog.validate(request)
        except UnknownAction as e:
            logger.warning("%s was refused: %s", subject, e)
            raise ApplicationError(str(e), type=UNKNOWN_ACTION, non_retryable=True) from None
        except InvalidActionPayload as e:
            logger.warning("%s was refused: %s", subject, e)
            raise ApplicationError(
                str(e), type=INVALID_ACTION_PAYLOAD, non_retryable=True
            ) from None


class _NotReserved(Exception):
    """A spend this activity refuses itself, in words it chose."""


def _spent(settlement: SpendSettlement, subject: str) -> Decimal:
    """What the handler said it spent, or the estimate when it said nothing that is an amount."""
    if settlement.reported_usd is not None:
        try:
            return usd(settlement.reported_usd, "the amount spent")
        except InvalidAmount:
            # The handler's result is the agent's, and its value is not logged: a defect to fix in
            # the handler, and the estimate is what the ledger can stand by meanwhile.
            logger.warning("%s: the handler reported a cost that is not an amount", subject)
    return usd(settlement.estimate_usd, "the estimate")


@contextmanager
def _refusals(subject: str, *, as_type: str) -> Iterator[None]:
    """Turns a spend that cannot be reserved or settled for the run's project into a failure for
    good, of ``as_type``. The messages name the action, the project or the rule at fault, never a
    payload."""
    try:
        yield
    except (
        _NotReserved,
        ProjectNotFound,
        InvalidAmount,
        StoredCreativeBudgetInvalid,
        CreativeBudgetEnvironmentInvalid,
        ReservationNotFound,
        SpendAlreadySettled,
    ) as e:
        logger.warning("%s was refused: %s", subject, e)
        raise ApplicationError(str(e), type=as_type, non_retryable=True) from None
