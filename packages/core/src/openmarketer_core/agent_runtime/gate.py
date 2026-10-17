"""The policy gate: whether an action an agent asked for runs, or waits for a person first.

Every action passes the gate where its workflow starts it (the worker's ``driver.py``), which is
the one place every action passes, so no node of a graph can skip it and no model can avoid it.
The gate reads only what the code declared about the action (``ActionPolicy.effect``), never the
request, whose name and payload are text a model may have shaped. An action that works for the
run itself runs; one that publishes or spends becomes a *proposal* that a person approves or
rejects from the dashboard, and that nobody decided on before its deadline is not run either.

An action that spends is weighed by the creative budget first (``budget/caps.py``, through the
ledger of ``budget/ledger.py``): its estimate is reserved, and the ledger's decision is the
verdict. A small spend within the month's cap runs without a person (``RUN``), a larger one waits
for a person with its estimate held (``ASK_A_PERSON``), and one that would take the month past
its cap is refused (``REFUSE``): no person can approve it. So ``verdict_for`` is given the
ledger's decision for such an action, and refuses to guess without it; ``may_wait_for_a_person``
says, from the declaration alone, whether an action can ever be held, which is what an agent's
workflow and registration must be ready for.

This is the gate of the design report's section "Policy gate" as far as it is built: it holds
every action that publishes for a person, and weighs every action that spends against the
creative budget. Banned content, a budget for publishing, the content filter, risk levels and
the project's autonomy level are not here yet; each will add a reason to refuse or to let an
action through before a person is asked.

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
from openmarketer_core.budget.caps import SpendDecision

# The signal the API sends to a run's workflow once a person decided on one of its proposals.
# Its argument is the proposal's id as text.
PROPOSAL_DECIDED_SIGNAL = "proposal_decided"


class GateVerdict(StrEnum):
    RUN = "run"
    ASK_A_PERSON = "ask_a_person"
    REFUSE = "refuse"  # a spend past the month's cap: it does not run, and nobody is asked


class SpendNotDecided(ValueError):
    """An action that spends was given no decision of the ledger to take its verdict from."""

    def __init__(self) -> None:
        super().__init__(
            "an action that spends is weighed by the creative budget: give verdict_for the "
            "ledger's decision on its estimate"
        )


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


# What each decision of the creative budget makes of an action that spends.
_VERDICT_OF_A_SPEND = {
    SpendDecision.RUN_AUTOMATICALLY: GateVerdict.RUN,
    SpendDecision.ASK_A_PERSON: GateVerdict.ASK_A_PERSON,
    SpendDecision.REFUSE: GateVerdict.REFUSE,
}


def may_wait_for_a_person(policy: ActionPolicy) -> bool:
    """Whether an action with ``policy`` can ever be held for a person: one that publishes or
    spends. Its workflow needs a project to propose it in, and its worker the database."""
    # Compared by value: the workflow sandbox loads its own copy of this module, and a policy
    # the worker's process made holds the other copy's member.
    return policy.effect != ActionEffect.INTERNAL


def spends(policy: ActionPolicy) -> bool:
    """Whether an action with ``policy`` spends, and so is weighed by the creative budget."""
    return policy.effect == ActionEffect.SPENDS


def verdict_for(policy: ActionPolicy, spend: SpendDecision | str | None = None) -> GateVerdict:
    """Whether an action with ``policy`` runs at once, waits for a person, or is refused.

    ``spend`` is the ledger's decision on the estimate of an action that spends (a
    ``SpendDecision`` or its value, as it crosses the workflow's history), and is ignored for
    any other action. Raises ``SpendNotDecided`` for an action that spends without one, and
    ``ValueError`` for a value that is not a decision.
    """
    if policy.effect == ActionEffect.INTERNAL:
        return GateVerdict.RUN
    if policy.effect == ActionEffect.PUBLISHES:
        return GateVerdict.ASK_A_PERSON
    if spend is None:
        raise SpendNotDecided
    return _VERDICT_OF_A_SPEND[SpendDecision(str(spend))]
