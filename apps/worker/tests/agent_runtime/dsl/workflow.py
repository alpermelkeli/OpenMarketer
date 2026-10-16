"""The greeter's workflows, with their hooks; the agent is imported the way a module usually does.

The sandbox loads this module, so it imports the agent's declarations and the decorator, and
never the graph or the wiring. The hooks note what they were given with an activity of their
own, which the worker registers (``wiring.py``) and this module calls by name. Each class is one
case: ``Greeter`` takes the run itself and sets nothing, ``GreeterWithLimitsOfItsOwn`` takes its
limits from an activity, ``GreeterOfARequest`` is started with an input of its own, and
``GreeterInItsOwnWords`` tells its failures in words of its own.
"""

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from dsl.definitions import (
    GREETER_TEXTS,
    LIMITS_OF_THE_RUN,
    NOTE,
    NOTE_REFUSED,
    GreetRequest,
    Limits,
    Note,
    agent,
    run_of_the_request,
    the_greeters_project,
)
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_worker.agent_runtime.vocabulary import (
    WORDED_BY_THE_RUNTIME,
    GraphRunPolicy,
)
from openmarketer_worker.agent_runtime.workflow_dsl import agent_workflow

GRAPH = GraphRunPolicy(
    attempt_timeout_seconds=31,
    heartbeat_timeout_seconds=13,
    max_attempts=2,
    retry_after_seconds=0.2,
)


async def note(step: str, detail: str) -> None:
    await workflow.execute_activity(
        NOTE,
        Note(step, detail),
        start_to_close_timeout=timedelta(seconds=10),
        # A note that fails is a failure of the hook: a test asks for it, once.
        retry_policy=RetryPolicy(maximum_attempts=1),
    )


@agent_workflow(agent, name="Greeter", graph=GRAPH, scope_of=the_greeters_project)
class Greeter:
    async def before(self, run: GraphRunRef) -> None:
        await note("before", run.kind)

    async def after(self, run: GraphRunRef, output: dict[str, Any]) -> None:
        await note("after", output["summary"])

    async def failed(self, run: GraphRunRef, reason: str) -> None:
        await note("failed", reason)


@agent_workflow(agent, name="GreeterWithLimitsOfItsOwn", graph=GRAPH, scope_of=the_greeters_project)
class GreeterWithLimitsOfItsOwn:
    """Takes the limits of its steps from an activity, as workflow code cannot read settings."""

    async def before(self, run: GraphRunRef) -> GraphRunPolicy:
        limits = await workflow.execute_activity(
            LIMITS_OF_THE_RUN, result_type=Limits, start_to_close_timeout=timedelta(seconds=10)
        )
        return GraphRunPolicy(
            attempt_timeout_seconds=limits.attempt_timeout_seconds,
            heartbeat_timeout_seconds=GRAPH.heartbeat_timeout_seconds,
            max_attempts=GRAPH.max_attempts,
            retry_after_seconds=GRAPH.retry_after_seconds,
        )


@agent_workflow(
    agent,
    name="GreeterOfARequest",
    graph=GRAPH,
    scope_of=the_greeters_project,
    input=GreetRequest,
    run_of=run_of_the_request,
)
class GreeterOfARequest:
    """Started with a request, as the API starts a workflow; every hook is given the request."""

    async def before(self, request: GreetRequest) -> GraphRunPolicy:
        await note("before", request.requested_by)
        return GraphRunPolicy(
            attempt_timeout_seconds=request.patience_seconds,
            heartbeat_timeout_seconds=GRAPH.heartbeat_timeout_seconds,
            max_attempts=GRAPH.max_attempts,
            retry_after_seconds=GRAPH.retry_after_seconds,
        )

    async def after(self, request: GreetRequest, output: dict[str, Any]) -> None:
        await note("after", f"{request.requested_by}: {output['summary']}")

    async def failed(self, request: GreetRequest, reason: str) -> None:
        await note("failed", f"{request.requested_by}: {reason}")


@agent_workflow(
    agent,
    name="GreeterInItsOwnWords",
    graph=GRAPH,
    scope_of=the_greeters_project,
    texts=GREETER_TEXTS,
    worded_types=(*WORDED_BY_THE_RUNTIME, NOTE_REFUSED),
)
class GreeterInItsOwnWords:
    async def before(self, run: GraphRunRef) -> None:
        await note("before", run.kind)

    async def failed(self, run: GraphRunRef, reason: str) -> None:
        await note("failed", reason)
