"""``agent_workflow``: an agent's workflow, written as a class that holds only what is its own.

Every agent's workflow does the same around its own steps: run the graph with the actions the
agent declared, say how a run that did not finish ended, and forget the run's checkpoints
whatever happened. ``agent_workflow`` writes that, so the author's class is what is particular
to the agent and nothing else. It may define three optional hooks, all ``async def``:

- ``before(self, run)``: first, with the run's ``GraphRunRef``. For marking the run started.
- ``after(self, output)``: once the graph finished, with the ``result`` its agent reports
  (``AgentSteps``). For storing what the run produced.
- ``failed(self, run, reason)``: when the run did not finish, with the sentence that may be
  shown to whoever asked for it. For recording why. It is called for a failure of ``before``,
  of the graph or of ``after``, and when the workflow was cancelled; it is shielded, so a
  cancelled run records itself. It is not called for a run that finished.

Then, in every case, the run's checkpoints are forgotten with the ``forget_run`` activity,
shielded as well. A failure of that activity is logged and does not replace the run's own
outcome: the rows are left to a cleanup. The class does not define ``run``: the decorator does,
and refuses a class that does, so that no workflow of an agent can start a run without the
actions it declared, the hooks and the forgetting.

The hooks are workflow code, which the sandbox loads and replays: deterministic, with no
network, file, clock, randomness or environment, and every side effect an activity. The
activities they call word their failures (``activity_support.worded_failures``), as the
runtime's own do: the reason ``failed`` gets is the message of the failure that ended the run,
and a message written for a log must not reach it.

``graph`` (a ``GraphRunPolicy``) and the agent's action policies are constants of the agent's
modules, never a workflow's input: they validate their values when they are made, and a
``ValueError`` while Temporal decodes a workflow's input fails the workflow task, which is
tried again for ever. What the driver refuses (an action the agent did not declare, too many
actions) is an ``ApplicationError`` of the workflow, not of an activity, and ends the run like
any other failure: ``failed`` hears of it.

``name`` is how the workflow is started (``client.start_workflow(name, run, ...)``), and what the
API and the worker share. The workflow id and the task queue are the starter's, and are decided
here by nobody. The class must be defined at the top of a module that the sandbox can load, so
it reaches the agent's declarations (``definitions.py``) and never the graph or the database.
Nothing here records anything on a run, holds an agent's words or reads an agent's settings:
that is the hooks' and the agent's.

Temporal accepts a ``run`` method only when its qualified name is that of a method of the
decorated class, and the decorator builds ``run`` itself: so it gives the function that name
(``test_workflow_dsl.py`` pins both checks of Temporal this relies on). Its annotations are
real types, not text, because Temporal reads them as the workflow's input and result.
"""

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError, FailureError

with workflow.unsafe.imports_passed_through():
    from openmarketer_core.agent_runtime.agent import Agent
    from openmarketer_core.agent_runtime.steps import GraphRunRef
    from openmarketer_worker.agent_runtime.driver import run_graph_with_actions
    from openmarketer_worker.agent_runtime.vocabulary import (
        FORGET_RUN,
        FORGET_RUN_LIMITS,
        RUN_TEXTS,
        STEP_REFUSED,
        GraphRunPolicy,
    )
    from openmarketer_worker.agent_runtime.workflow_support import database_write, reason_shown

# The hooks a class may define, with what each is called with, in order.
_HOOKS: dict[str, tuple[str, ...]] = {
    "before": ("run",),
    "after": ("output",),
    "failed": ("run", "reason"),
}

# What ends a run and so is told to ``failed``: every failure Temporal ends a workflow with (an
# activity that failed, the driver's own refusals, a child workflow or a timeout of a hook's)
# and a cancellation. Anything else is a defect of the workflow's code, which fails the
# workflow task and is tried again once the code is fixed: that run is not over.
_RUN_FAILURES = (FailureError, asyncio.CancelledError)


class InvalidAgentWorkflow(ValueError):
    """A class cannot be the workflow of an agent, or the workflow is declared without a name."""


def agent_workflow[Workflow](
    agent: Agent, *, name: str, graph: GraphRunPolicy
) -> Callable[[type[Workflow]], type[Workflow]]:
    """Make the class it decorates the Temporal workflow ``name`` that runs ``agent``.

    The class gets a ``run(self, run_ref: GraphRunRef) -> dict[str, Any]``: the output is what
    the agent's graph finished with. Raises ``InvalidAgentWorkflow`` when the class defines
    ``run`` or a hook that is not an ``async def`` taking what its hook is given, or is not
    defined at the top of a module, or when ``name`` is empty.
    """
    if not name:
        raise InvalidAgentWorkflow(
            "an agent's workflow needs a name: it is how the workflow is started"
        )

    def decorate(cls: type[Workflow]) -> type[Workflow]:
        _require_a_class_that_can_be_a_workflow(cls)

        async def run(self, run_ref: GraphRunRef) -> dict[str, Any]:
            return await _run_agent(self, run_ref, agent=agent, policy=graph)

        # Temporal checks the qualified name and the module of ``run`` against the class.
        run.__qualname__ = f"{cls.__qualname__}.run"
        run.__module__ = cls.__module__
        # The class has no ``run`` for the type checker: that is the point of the decorator.
        cls.run = workflow.run(run)  # pyright: ignore[reportAttributeAccessIssue]
        return workflow.defn(name=name)(cls)

    return decorate


def _require_a_class_that_can_be_a_workflow(cls: type) -> None:
    if cls.__qualname__ != cls.__name__:
        raise InvalidAgentWorkflow(
            f"workflow class {cls.__name__} is defined inside a function or another class: "
            "Temporal reaches a workflow by the name of its module and class, so define it "
            "at the top of a module"
        )
    if hasattr(cls, "run"):
        raise InvalidAgentWorkflow(
            f"workflow class {cls.__name__} defines run, which agent_workflow writes: put what "
            "is particular to the agent in the hooks before, after and failed"
        )
    for hook, arguments in _HOOKS.items():
        _require_a_hook_that_can_be_called(cls, hook, arguments)


def _require_a_hook_that_can_be_called(cls: type, hook: str, arguments: tuple[str, ...]) -> None:
    defined = getattr(cls, hook, None)
    if defined is None:
        return
    shape = f"async def {hook}(self, {', '.join(arguments)})"
    if not inspect.iscoroutinefunction(defined):
        raise InvalidAgentWorkflow(
            f"hook {hook} of workflow class {cls.__name__} must be an async def: {shape}"
        )
    try:
        inspect.signature(defined).bind(None, *arguments)
    except TypeError:
        raise InvalidAgentWorkflow(
            f"hook {hook} of workflow class {cls.__name__} must take what it is given: {shape}"
        ) from None


async def _run_agent(
    instance: object, run: GraphRunRef, *, agent: Agent, policy: GraphRunPolicy
) -> dict[str, Any]:
    try:
        await _call_hook(instance, "before", run)
        output = await run_graph_with_actions(run, actions=agent.policies, policy=policy)
        await _call_hook(instance, "after", output)
    except _RUN_FAILURES as failure:
        try:
            failed = getattr(instance, "failed", None)
            if failed is not None:
                # Shielded: when the workflow was cancelled, the record must still be written.
                await asyncio.shield(failed(run, reason_shown(failure, RUN_TEXTS)))
        finally:
            await _forget(run)
        raise
    await _forget(run)
    return output


async def _call_hook(instance: object, hook: str, argument: Any) -> None:
    defined = getattr(instance, hook, None)
    if defined is not None:
        await defined(argument)


async def _forget(run: GraphRunRef) -> None:
    try:
        # Shielded for the same reason as ``failed``: a cancelled run is over too.
        await asyncio.shield(
            database_write(FORGET_RUN, run, FORGET_RUN_LIMITS, not_retried=[STEP_REFUSED])
        )
    except ActivityError as failure:
        # The outcome of the run is what its starter reads; rows left behind are not worth
        # replacing it.
        workflow.logger.warning(
            "the checkpoints of %s run %s were not forgotten: %s",
            run.kind,
            run.run_id,
            reason_shown(failure, RUN_TEXTS),
        )
