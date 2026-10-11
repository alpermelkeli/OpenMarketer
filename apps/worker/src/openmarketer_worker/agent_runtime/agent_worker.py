"""``AgentWorker``: the handlers of an agent's actions, and what the worker registers for it.

An agent declares its actions in ``definitions.py`` (``Agent.action``: a name, a payload model
and the limits it runs under). ``AgentWorker`` is where it says what runs each one:

    worker = AgentWorker(agent, task_queue="campaigns")

    @worker.activity(SendEmail)
    def send_email(payload: SendEmail, context: ActionContext) -> Mapping[str, Any]: ...

A handler is blocking and runs in a thread, with the payload as the model it was declared for
(already read as strict JSON) and ``ActionContext.action_id``, the idempotency key of its side
effect; ``routes.py`` and ``activities.py`` say the rest. The decorator returns the handler
as it is, so it is tested as a plain function.

``registration`` turns the handlers into what the process serves: the runtime's activities
(``run_graph_step``, ``run_action`` and ``forget_run``) with the agent's routes and graph, the
agent's own activities (the ones its workflow's hooks call) and its workflows. Whatever can be
wrong is wrong when the worker starts, not at the first run: an action with no handler, a handler
for a model the agent did not declare or for an action that has one already, a workflow class
that Temporal does not know. The errors name the action, the model or the class, never a payload.

This module is for the worker process: it imports the activities and the database, and the
workflow sandbox never loads it. It runs no action and builds no graph: the agent's wiring hands
in its ``AgentGraph`` (core's ``AgentSteps``) and its ``CheckpointAccess``.
"""

import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from pydantic import BaseModel
from temporalio import workflow

from openmarketer_core.agent_runtime.agent import Agent
from openmarketer_worker import AgentRegistration
from openmarketer_worker.agent_runtime.activities import AgentGraph, RuntimeActivities
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess
from openmarketer_worker.agent_runtime.routes import (
    ActionContext,
    ActionHandler,
    ActionRoute,
    ActionRoutes,
)
from openmarketer_worker.checkpoint_cleanup import CheckpointCleanup

type Handler[Payload: BaseModel] = Callable[[Payload, ActionContext], Mapping[str, Any]]


class InvalidAgentWorker(ValueError):
    """The handlers, activities or workflows of an agent do not fit the agent that declared them."""


class AgentWorker:
    """The handlers of one agent's actions, collected with a decorator."""

    def __init__(self, agent: Agent, *, task_queue: str) -> None:
        """``task_queue`` is where the agent's workflows are started."""
        self._agent = agent
        self._task_queue = task_queue
        self._handlers: dict[str, ActionHandler] = {}

    def activity[Payload: BaseModel](
        self, payload: type[Payload]
    ) -> Callable[[Handler[Payload]], Handler[Payload]]:
        """Make the function it decorates the handler of the action ``payload`` was declared for.

        Raises ``InvalidAgentWorker`` for a model the agent did not declare as an action, here
        and not when the function is decorated, and for an action that has a handler already
        or a handler that is an ``async def`` (it runs in a thread and returns its result).
        """
        action = self._action_declared_for(payload)

        def register(handler: Handler[Payload]) -> Handler[Payload]:
            if inspect.iscoroutinefunction(handler):
                raise InvalidAgentWorker(
                    f"handler of action {action!r} is an async def: a handler blocks and runs "
                    "in a thread, and returns its result"
                )
            taken = self._handlers.get(action)
            if taken is not None:
                raise InvalidAgentWorker(
                    f"action {action!r} of agent {self._agent.kind!r} has two handlers: "
                    f"{_named(taken)} and {_named(handler)}"
                )
            # The model and the handler are paired by this decorator, and ``run_action`` reads
            # the payload as that model before it calls the handler: the one place that knows
            # the pair is the one place the handler's parameter may be widened to the base.
            self._handlers[action] = cast(ActionHandler, handler)
            return handler

        return register

    def registration(
        self,
        *,
        checkpoints: CheckpointAccess,
        graph: AgentGraph,
        workflows: Sequence[type],
        activities: Sequence[Callable[..., Any]] = (),
        checkpoint_cleanups: Sequence[CheckpointCleanup] = (),
    ) -> AgentRegistration:
        """What the worker registers for the agent: its workflows and every activity they call.

        ``workflows`` are the classes ``agent_workflow`` made, and ``activities`` the agent's
        own (the ones its hooks call). Raises ``InvalidAgentWorker`` for an action with no
        handler and for a class that is not a Temporal workflow.
        """
        routes = self._routes()
        for workflow_class in workflows:
            _require_a_workflow(workflow_class)
        runtime = RuntimeActivities(checkpoints, graph, routes)
        return AgentRegistration(
            task_queue=self._task_queue,
            workflows=list(workflows),
            activities=[
                runtime.run_graph_step,
                runtime.run_action,
                runtime.forget_run,
                *activities,
            ],
            checkpoint_cleanups=list(checkpoint_cleanups),
        )

    def _action_declared_for(self, payload: type[BaseModel]) -> str:
        for definition in self._agent.definitions:
            if definition.payload is payload:
                return definition.name
        raise InvalidAgentWorker(
            f"model {payload.__name__} is not the payload of an action of agent "
            f"{self._agent.kind!r}: declare it with @agent.action"
        )

    def _routes(self) -> ActionRoutes:
        without_handler = [
            definition.name
            for definition in self._agent.definitions
            if definition.name not in self._handlers
        ]
        if without_handler:
            raise InvalidAgentWorker(
                f"agent {self._agent.kind!r} declares "
                f"{', '.join(repr(name) for name in without_handler)} with no handler: "
                "register one with @worker.activity"
            )
        return ActionRoutes(
            ActionRoute(definition, self._handlers[definition.name])
            for definition in self._agent.definitions
        )


def _named(function: Callable[..., Any]) -> str:
    return getattr(function, "__qualname__", repr(function))


def _require_a_workflow(candidate: type) -> None:
    # Temporal's own test of whether a class was decorated with ``workflow.defn``: the worker
    # applies it when it is built, and a class that fails it fails there with a longer story.
    if workflow._Definition.from_class(candidate) is None:  # pyright: ignore[reportPrivateUsage]
        raise InvalidAgentWorker(
            f"{getattr(candidate, '__name__', repr(candidate))} is not a Temporal workflow: "
            "make it one with @agent_workflow"
        )
