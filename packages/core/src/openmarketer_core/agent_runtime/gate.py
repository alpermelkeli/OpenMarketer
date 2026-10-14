"""The policy gate: whether an action an agent asked for runs, or waits for a person first.

Every action passes the gate where its workflow starts it (the worker's ``driver.py``), which is
the one place every action passes, so no node of a graph can skip it and no model can avoid it.
The gate reads only what the code declared about the action (``ActionPolicy.effect``), never the
request, whose name and payload are text a model may have shaped. An action that works for the
run itself runs; one that publishes or spends becomes a *proposal* that a person approves or
rejects from the dashboard, and that nobody decided on before its deadline is not run either.

This is the first, smallest gate of the design report's section "Policy gate": it holds every
action that publishes or spends for a person. Banned content, budget caps, the content filter,
risk levels and the project's autonomy level are not here yet; each will add a reason to
refuse or to let an action through before a person is asked.

What a proposal is and how its decision travels are here too, as plain values, because the
workflow, the worker's activities and the API share them: the statuses of a proposal, where it
belongs (``RunScope``) and the name of the signal that tells a workflow a person decided. The
decision itself is in the database (``db/proposals.py``): the signal only wakes the workflow,
which then reads it, so a signal carries no authority of its own.

This module imports no workflow engine, database or framework: the workflow sandbox loads it.
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum

from openmarketer_core.agent_runtime.actions import ActionEffect, ActionPolicy

# The signal the API sends to a run's workflow once a person decided on one of its proposals.
# Its argument is the proposal's id as text.
PROPOSAL_DECIDED_SIGNAL = "proposal_decided"


class GateVerdict(StrEnum):
    RUN = "run"
    ASK_A_PERSON = "ask_a_person"


class ProposalStatus(StrEnum):
    """Where a proposal stands. It leaves ``PENDING`` once and never changes again."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"  # nobody decided before its deadline, or its run ended first


@dataclass(frozen=True)
class RunScope:
    """The workspace and the project a run works for, which its proposals belong to."""

    workspace_id: uuid.UUID
    project_id: uuid.UUID


def verdict_for(policy: ActionPolicy) -> GateVerdict:
    """Whether an action with ``policy`` runs at once or waits for a person's approval."""
    # Compared by value: the workflow sandbox loads its own copy of this module, and a policy
    # the worker's process made holds the other copy's member.
    if policy.effect == ActionEffect.INTERNAL:
        return GateVerdict.RUN
    return GateVerdict.ASK_A_PERSON
