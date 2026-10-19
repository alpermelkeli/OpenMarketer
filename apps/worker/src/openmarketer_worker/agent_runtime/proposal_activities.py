"""The policy gate's activities: storing a proposal, reading it back, and checking an approval.

``propose_action`` stores an action the gate held as a pending proposal and says how long is
left until its deadline; ``settle_proposal`` reads where it stands and expires it once the
deadline passed (``approval_wait.py`` calls both). ``is_approved`` is not an activity: it is how
``run_action`` checks, before it runs the handler of an action the gate holds, that a person
approved that very action, so that a workflow that skipped the gate (one started by hand on the
workflow server, say) still runs nothing a person did not approve.

They hold no rule: the proposal's transitions are core's ``db/proposals.py``. Each activity is
one short transaction and safe to repeat: storing the same action again returns the proposal
that is there, and settling a proposal that left ``pending`` changes nothing. A proposal that
cannot be stored or found for the run's project fails for good (``PROPOSAL_REFUSED``), worded
with the identifiers at fault; any other failure is worded by ``worded_failures``.

This module is for the worker process: the workflow sandbox never loads it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from openmarketer_core.agent_runtime.actions import ActionEffect
from openmarketer_core.agent_runtime.gate import ProposalStatus
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.proposals import (
    ProposalNotFound,
    proposal_status_of_action,
    propose_action,
    settle_proposal,
)
from openmarketer_core.db.session import transaction
from openmarketer_worker.agent_runtime.activity_support import worded_failures
from openmarketer_worker.agent_runtime.vocabulary import (
    ACTION_TEXTS,
    PROPOSAL_REFUSED,
    PROPOSE_ACTION,
    SETTLE_PROPOSAL,
    ProposalHeld,
    ProposalInput,
    SettleInput,
)

logger = logging.getLogger(__name__)


class ProposalActivities:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @activity.defn(name=PROPOSE_ACTION)
    def propose_action(self, proposal: ProposalInput) -> ProposalHeld:
        """Store the action as a pending proposal, or find the one stored for it before."""
        subject = f"proposal of action {proposal.request.action_id}"
        with worded_failures(ACTION_TEXTS, subject=subject), _refusals(subject):
            with transaction(self._sessions) as session:
                stored = propose_action(
                    session,
                    scope=proposal.scope,
                    run=proposal.run,
                    workflow_id=proposal.workflow_id,
                    request=proposal.request,
                    effect=ActionEffect(proposal.effect),
                    wait_seconds=proposal.wait_seconds,
                    estimate_usd=(
                        None if proposal.estimate_usd is None else Decimal(proposal.estimate_usd)
                    ),
                )
            left = (stored.expires_at - datetime.now(UTC)).total_seconds()
            return ProposalHeld(proposal_id=stored.id, seconds_left=max(left, 0.0))

    @activity.defn(name=SETTLE_PROPOSAL)
    def settle_proposal(self, settle: SettleInput) -> str:
        """Where the proposal stands, as the value of its ``ProposalStatus``."""
        subject = f"proposal of action {settle.action_id}"
        with worded_failures(ACTION_TEXTS, subject=subject), _refusals(subject):
            with transaction(self._sessions) as session:
                status = settle_proposal(
                    session,
                    scope=settle.scope,
                    action_id=settle.action_id,
                    deadline_passed=settle.deadline_passed,
                )
            return status.value

    def is_approved(self, action_id: str) -> bool:
        """Whether a person approved the proposal of ``action_id``. It blocks."""
        with transaction(self._sessions) as session:
            return proposal_status_of_action(session, action_id) is ProposalStatus.APPROVED


@contextmanager
def _refusals(subject: str) -> Iterator[None]:
    """Turns a proposal that cannot belong to the run's project into a failure for good."""
    try:
        yield
    except (ProposalNotFound, ProjectNotFound) as e:
        logger.warning("%s was refused: %s", subject, e)
        # The messages name the project and the proposal by their ids, and nothing else.
        raise ApplicationError(str(e), type=PROPOSAL_REFUSED, non_retryable=True) from None
