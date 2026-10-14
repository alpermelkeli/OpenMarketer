"""Deciding on a proposal: store the decision, then tell the run that waits for it.

The order is the point. The decision is committed before the run is told, because the run reads
it from the database when it wakes (the worker's ``approval_wait.py``); a run told first would
find the proposal still pending and wait again. A run that cannot be told (the workflow server is
down, the run's workflow ended) still finds the decision when it reads the proposal again on its
own, within the worker's recheck interval, so a failure to tell it is logged and not raised: the
decision stands.

How a run is told is the caller's business; it passes a function. This module decides nothing
about who may decide: the decider is whatever user id the caller authenticated.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.proposals import ActionProposal, decide_proposal
from openmarketer_core.db.session import transaction

logger = logging.getLogger(__name__)


class RunNotTold(Exception):
    """The run's workflow could not be told of a decision. Raised by the function that tells it."""


def decide_on_proposal(
    sessions: sessionmaker[Session],
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    proposal_id: uuid.UUID,
    approved: bool,
    decided_by: uuid.UUID,
    tell_run: Callable[[str, uuid.UUID], None],
) -> ActionProposal:
    """Approve or reject a pending proposal as ``decided_by``, and tell its run.

    ``tell_run`` is called with the run's workflow id and the proposal's id. Raises
    ``ProposalNotFound`` and ``ProposalNotPending``.
    """
    with transaction(sessions) as session:
        proposal = decide_proposal(
            session,
            workspace_id=workspace_id,
            project_id=project_id,
            proposal_id=proposal_id,
            approved=approved,
            decided_by=decided_by,
        )
    try:
        tell_run(proposal.workflow_id, proposal.id)
    except RunNotTold as e:
        logger.warning(
            "the run of proposal %s was not told of the decision and reads it later: %s",
            proposal.id,
            e,
        )
    return proposal
