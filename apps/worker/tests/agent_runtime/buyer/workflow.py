"""The buyer's workflow: no hooks, the scope its spends belong to, and the studio's words."""

from buyer.definitions import STUDIO_REFUSED, BuyerRun, agent, run_of, scope_of
from openmarketer_worker.agent_runtime.vocabulary import WORDED_BY_THE_RUNTIME, GraphRunPolicy
from openmarketer_worker.agent_runtime.workflow_dsl import agent_workflow

GRAPH = GraphRunPolicy(
    attempt_timeout_seconds=30,
    heartbeat_timeout_seconds=10,
    max_attempts=2,
    retry_after_seconds=0.2,
)


@agent_workflow(
    agent,
    name="Buyer",
    graph=GRAPH,
    input=BuyerRun,
    run_of=run_of,
    scope_of=scope_of,
    worded_types=(*WORDED_BY_THE_RUNTIME, STUDIO_REFUSED),
)
class Buyer:
    """Everything a run does is the runtime's: the budget, the gate, the wait and the action."""
