"""The same agent's workflow with no hooks, and the agent imported through the sandbox.

``workflow.py`` imports the agent plainly, so the sandbox loads its own copy of it. Here the
agent is passed through, which is the other way a module may import it: the sandbox shares the
one object the worker's process has, and the workflow must see its policies all the same. The
sandbox loads a module once, so the two cases cannot share one.
"""

from temporalio import workflow

from openmarketer_worker.agent_runtime.vocabulary import GraphRunPolicy
from openmarketer_worker.agent_runtime.workflow_dsl import agent_workflow

with workflow.unsafe.imports_passed_through():
    from dsl.definitions import agent, the_greeters_project

GRAPH = GraphRunPolicy(
    attempt_timeout_seconds=32,
    heartbeat_timeout_seconds=14,
    max_attempts=3,
    retry_after_seconds=0.3,
)


@agent_workflow(agent, name="GreeterPassedThrough", graph=GRAPH, scope_of=the_greeters_project)
class GreeterPassedThrough:
    """Defines no hook: a class with none is a workflow too."""
