"""The greeter's workflow, with its hooks; the agent is imported the way a module usually does.

The sandbox loads this module, so it imports the agent's declarations and the decorator, and
never the graph or the handlers. The hooks note what they were given with an activity of their
own, which the worker registers (``handlers.py``) and this module calls by name.
"""

from datetime import timedelta
from typing import Any

from temporalio import workflow

from dsl.definitions import NOTE, Note, agent
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_worker.agent_runtime.vocabulary import GraphRunPolicy
from openmarketer_worker.agent_runtime.workflow_dsl import agent_workflow

GRAPH = GraphRunPolicy(
    attempt_timeout_seconds=31,
    heartbeat_timeout_seconds=13,
    max_attempts=2,
    retry_after_seconds=0.2,
)


async def note(step: str, detail: str) -> None:
    await workflow.execute_activity(
        NOTE, Note(step, detail), start_to_close_timeout=timedelta(seconds=10)
    )


@agent_workflow(agent, name="Greeter", graph=GRAPH)
class Greeter:
    async def before(self, run: GraphRunRef) -> None:
        await note("before", run.kind)

    async def after(self, output: dict[str, Any]) -> None:
        await note("after", output["summary"])

    async def failed(self, run: GraphRunRef, reason: str) -> None:
        await note("failed", reason)
