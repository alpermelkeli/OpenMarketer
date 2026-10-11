"""A workflow that passes the driver through the sandbox, as an agent's workflow module may.

``scripted_agent.py`` imports the driver plainly. This module imports it inside
``workflow.unsafe.imports_passed_through()``, which is the natural thing to write and so the one an
agent author will write: the sandbox then does not load the driver itself, and the types of core
that the workflow decodes must still be readable from where they were defined.

It stands apart from ``scripted_agent.py`` because the sandbox loads a module once: its imports
decide the case, so the two cases cannot share a module. It does not import ``scripted_agent`` for
the same reason.
"""

from dataclasses import dataclass
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from openmarketer_core.agent_runtime.steps import GraphRunRef
    from openmarketer_worker.agent_runtime.driver import run_graph_with_actions
    from openmarketer_worker.agent_runtime.vocabulary import ActionPolicy, GraphRunPolicy


@dataclass(frozen=True)
class PassedThroughRun:
    run: GraphRunRef
    actions: dict[str, ActionPolicy]
    policy: GraphRunPolicy


@workflow.defn
class PassesTheDriverThrough:
    @workflow.run
    async def run(self, scripted: PassedThroughRun) -> dict[str, Any]:
        return await run_graph_with_actions(
            scripted.run, actions=scripted.actions, policy=scripted.policy
        )
