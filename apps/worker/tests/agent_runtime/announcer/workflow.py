"""The announcer's workflow: no hooks, and the scope its proposals belong to."""

from announcer.definitions import AnnouncerRun, agent, run_of, scope_of
from openmarketer_worker.agent_runtime.vocabulary import GraphRunPolicy
from openmarketer_worker.agent_runtime.workflow_dsl import agent_workflow

GRAPH = GraphRunPolicy(
    attempt_timeout_seconds=30,
    heartbeat_timeout_seconds=10,
    max_attempts=2,
    retry_after_seconds=0.2,
)


@agent_workflow(
    agent, name="Announcer", graph=GRAPH, input=AnnouncerRun, run_of=run_of, scope_of=scope_of
)
class Announcer:
    """Everything a run does is the runtime's: the gate, the wait and the action."""
