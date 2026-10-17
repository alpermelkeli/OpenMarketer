"""``AgentWorker``: the handlers of an agent's actions, and what the worker registers for it.

An agent declares its actions in ``definitions.py`` (``Agent.action``: a name, a payload model
and the limits it runs under). ``AgentWorker`` is where it says what runs each one:

    worker = AgentWorker(agent, task_queue="campaigns")

    @worker.handler(SendEmail)
    def send_email(payload: SendEmail, context: ActionContext) -> Mapping[str, Any]: ...

    @worker.prepare_run(Research)
    def prepare_research(payload: Research, context: RunContext) -> uuid.UUID: ...

    @worker.estimate(GenerateImage)
    def estimate_image(payload: GenerateImage, context: SpendContext) -> Decimal: ...

    @worker.run_ended
    def release(run: GraphRunRef) -> None: ...

A handler is blocking and runs in a thread, with the payload as the model it was declared for
(already read as strict JSON) and ``ActionContext.action_id``, the idempotency key of its side
effect; ``routes.py`` and ``activities.py`` say the rest. It is not a Temporal activity, which
the agent's hooks call by name and which sits beside it in a registration. The decorator returns
the handler as it is, so it is tested as a plain function. An action with a ``RunTarget``
(``Agent.action(run=...)``) runs no handler: ``prepare_run`` registers the callable that
creates the child run's record, keyed by the action's id, and answers with its id; the
workflow starts and awaits the child workflow itself. ``run_ended`` is the one function
that releases what a run left outside the checkpoints (a folder on disk) once the run is over,
called by ``forget_run`` and blocking too. ``estimate`` is what an action that spends
(``ActionEffect.SPENDS``) is estimated to cost, in US dollars, from its payload: it blocks and
runs in the activity ``reserve_spend``, which reserves the estimate under the creative budget
before the handler may run, and every such action has one. Its handler reports what it spent in
its result's ``cost_usd`` (``vocabulary.COST_USD``), or is taken to have spent the estimate.

``registration`` turns the handlers into what the process serves: the runtime's activities
(``run_graph_step``, ``run_action``, ``prepare_run`` and ``forget_run``) with the agent's
routes, graph, kind and
words, the agent's own activities (the ones its workflow's hooks call) and its workflows.
An agent with an action the policy gate holds for a person also gives ``registration`` the
worker's database sessions: it registers the gate's activities (``propose_action`` and
``settle_proposal``) with them, and ``run_action`` checks each such action's approval there.
An agent with an action that spends gives the sessions and the process's environment too, and
gets the budget's activities (``reserve_spend`` and ``settle_spend``), which resolve the
workspace's caps at each attempt.
``registration`` is also given how long a run of the agent can take at most
(``longest_run``), and adds core's rule for its kind to the cleanups of left-over checkpoints
(``checkpoint_cleanup.stale_threads_of``), so that no agent's threads are left for nobody to
remove.

Whatever can be wrong is wrong when the worker starts, not at the first run: an action with no
handler, a run action with no prepare callable, an action that spends with no estimate, an
estimate for an action that does not spend, an action that waits for a person with no sessions
to store its proposal in, an action that spends with no environment to read the caps in, a
handler, prepare or estimate for a model the agent did not declare or for an action that has
one already, a ``longest_run`` that does not cover what an action can wait for, a workflow
class that is not one this agent's ``agent_workflow`` made. The errors name the action, the
model or the class, never a payload.

This module is for the worker process: it imports the activities and the database, and the
workflow sandbox never loads it. It runs no action and builds no graph: the agent's wiring hands
in its ``AgentGraph`` (core's ``AgentSteps``) and its ``CheckpointAccess``.
"""

import inspect
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from typing import Any, cast

from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.agent_runtime.actions import ActionCatalog, ActionDefinition, RunKind
from openmarketer_core.agent_runtime.agent import Agent
from openmarketer_core.agent_runtime.gate import may_wait_for_a_person, spends
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_worker import AgentRegistration, PeriodicTask
from openmarketer_worker.agent_runtime.activities import AgentGraph, RuntimeActivities
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess
from openmarketer_worker.agent_runtime.proposal_activities import ProposalActivities
from openmarketer_worker.agent_runtime.routes import (
    ActionContext,
    ActionHandler,
    ActionRoute,
    ActionRoutes,
    PrepareRun,
    RunRoute,
    RunRoutes,
    SpendEstimate,
)
from openmarketer_worker.agent_runtime.spend_activities import SpendActivities
from openmarketer_worker.agent_runtime.vocabulary import RUN_TEXTS, FailureTexts
from openmarketer_worker.agent_runtime.workflow_dsl import agent_of
from openmarketer_worker.checkpoint_cleanup import CheckpointCleanup, stale_threads_of

type Handler[Payload: BaseModel] = Callable[[Payload, ActionContext], Mapping[str, Any]]
type RunEnded = Callable[[GraphRunRef], None]


class InvalidAgentWorker(ValueError):
    """The handlers, activities or workflows of an agent do not fit the agent that declared them."""


class AgentWorker:
    """The handlers of one agent's actions, collected with a decorator."""

    def __init__(self, agent: Agent, *, task_queue: str) -> None:
        """``task_queue`` is where the agent's workflows are started."""
        self._agent = agent
        self._task_queue = task_queue
        self._handlers: dict[str, ActionHandler] = {}
        self._prepares: dict[str, PrepareRun] = {}
        self._estimates: dict[str, SpendEstimate] = {}
        self._run_ended: RunEnded | None = None

    def handler[Payload: BaseModel](
        self, payload: type[Payload]
    ) -> Callable[[Handler[Payload]], Handler[Payload]]:
        """Make the function it decorates the handler of the action ``payload`` was declared for.

        Raises ``InvalidAgentWorker`` for a model the agent did not declare as an action, here
        and not when the function is decorated, for an action that has a handler already, for a
        handler that is an ``async def`` (it runs in a thread and returns its result), and for
        an action that starts another agent's run: such an action has no handler of this
        worker, and ``prepare_run`` registers the callable that creates the child run instead.
        """
        definition = self._definition_of(payload)
        action = definition.name
        if definition.run is not None:
            raise InvalidAgentWorker(
                f"action {action!r} of agent {self._agent.kind!r} starts another run and runs "
                "no handler: register what creates the child run's record with @worker.prepare_run"
            )

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

    def prepare_run[Payload: BaseModel](
        self, payload: type[Payload]
    ) -> Callable[[PrepareRun[Payload]], PrepareRun[Payload]]:
        """Make the function it decorates the one that creates the run ``payload`` starts.

        It is given the validated payload and a ``RunContext`` whose ``action_id`` is the
        idempotency key of its work (a retried attempt passes the same one, and an
        implementation keyed by it finds the run it made), and answers with the child run's
        id. It blocks and runs in a thread. Raises ``InvalidAgentWorker`` for a model the
        agent did not declare, an action that is not a run action, a second prepare for one
        action, and an ``async def``.
        """
        definition = self._definition_of(payload)
        action = definition.name
        if definition.run is None:
            raise InvalidAgentWorker(
                f"action {action!r} of agent {self._agent.kind!r} runs as an activity of this "
                "worker: register what runs it with @worker.handler, not a prepare callable"
            )

        def register(prepare: PrepareRun[Payload]) -> PrepareRun[Payload]:
            if inspect.iscoroutinefunction(prepare):
                raise InvalidAgentWorker(
                    f"prepare callable of action {action!r} is an async def: it blocks and "
                    "runs in a thread, and returns the child run's id"
                )
            taken = self._prepares.get(action)
            if taken is not None:
                raise InvalidAgentWorker(
                    f"action {action!r} of agent {self._agent.kind!r} has two prepare "
                    f"callables: {_named(taken)} and {_named(prepare)}"
                )
            self._prepares[action] = cast(PrepareRun, prepare)
            return prepare

        return register

    def estimate[Payload: BaseModel](
        self, payload: type[Payload]
    ) -> Callable[[SpendEstimate[Payload]], SpendEstimate[Payload]]:
        """Make the function it decorates the estimate of the action ``payload`` was declared for.

        It is given the validated payload and a ``SpendContext`` (the action's id and the run's
        workspace and project), and answers with what the action will cost in US dollars, which
        the creative budget decides on before the handler may run. It blocks and runs in an
        activity. Raises ``InvalidAgentWorker`` for a model the agent did not declare, an action
        that does not spend (``ActionEffect.SPENDS``), a second estimate for one action, and an
        ``async def``.
        """
        definition = self._definition_of(payload)
        action = definition.name
        if not spends(self._agent.policies[action]):
            raise InvalidAgentWorker(
                f"action {action!r} of agent {self._agent.kind!r} does not spend: only an action "
                "declared with effect=ActionEffect.SPENDS has an estimate"
            )

        def register(estimate: SpendEstimate[Payload]) -> SpendEstimate[Payload]:
            if inspect.iscoroutinefunction(estimate):
                raise InvalidAgentWorker(
                    f"estimate of action {action!r} is an async def: it blocks and runs in an "
                    "activity, and returns the amount"
                )
            taken = self._estimates.get(action)
            if taken is not None:
                raise InvalidAgentWorker(
                    f"action {action!r} of agent {self._agent.kind!r} has two estimates: "
                    f"{_named(taken)} and {_named(estimate)}"
                )
            self._estimates[action] = cast(SpendEstimate, estimate)
            return estimate

        return register

    def run_ended(self, function: RunEnded) -> RunEnded:
        """Make the function it decorates the one that releases what a run left once it is over.

        It is called with the run, after the run's checkpoints were forgotten, for a run that
        finished, failed or was cancelled; it blocks and runs in a thread, may be called again
        for a run (an activity may run again), and a failure of it is logged and not raised.
        Raises ``InvalidAgentWorker`` for a second one and for an ``async def``.
        """
        if inspect.iscoroutinefunction(function):
            raise InvalidAgentWorker(
                f"the run-ended function of agent {self._agent.kind!r} is an async def: "
                "it blocks and runs in a thread"
            )
        if self._run_ended is not None:
            raise InvalidAgentWorker(
                f"agent {self._agent.kind!r} has two run-ended functions: "
                f"{_named(self._run_ended)} and {_named(function)}"
            )
        self._run_ended = function
        return function

    def registration(
        self,
        *,
        checkpoints: CheckpointAccess,
        graph: AgentGraph,
        workflows: Sequence[type],
        texts: FailureTexts = RUN_TEXTS,
        activities: Sequence[Callable[..., Any]] = (),
        longest_run: timedelta,
        checkpoint_cleanups: Sequence[CheckpointCleanup] = (),
        periodic_tasks: Sequence[PeriodicTask] = (),
        sessions: sessionmaker[Session] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> AgentRegistration:
        """What the worker registers for the agent: its workflows and every activity they call.

        ``workflows`` are the classes this agent's ``agent_workflow`` made, and ``activities``
        the agent's own (the ones its hooks call). ``texts`` are the agent's words for a run's
        failures that nobody worded, the same its workflows were given: the runtime's by
        default. ``longest_run`` is the most a run can take from its first step to its end, all
        retries and waits for a person included: a thread of the agent's kind that nothing wrote
        for longer is removed, and a bound that does not cover a wait for a person a run may
        make, or a run a waiting action may wait for, is refused. ``checkpoint_cleanups`` are
        rules of the agent's own, beside that one. ``periodic_tasks`` are schedules of the
        agent's own, which the process runs while it serves (the researcher's page
        retention). ``sessions`` are the database's, for an
        agent with an action that waits for a person or spends, and ``environ`` the process's
        environment, for an agent with an action that spends: a cap set there wins over the
        workspace's choice. Raises ``InvalidAgentWorker`` for an action with no handler, a run
        action with no prepare callable or an action that spends with no estimate, for an
        action that waits for a person without ``sessions``, for one that spends without
        ``environ``, for the bounds above, and for a class that is not a workflow of this agent.
        """
        self._require_a_run_that_outlasts_its_waits(longest_run)
        routes = self._routes()
        run_routes = self._run_routes()
        for workflow_class in workflows:
            self._require_a_workflow_of_this_agent(workflow_class)
        budget = self._budget_of(sessions, environ)
        gate = self._gate_of(sessions)
        runtime = RuntimeActivities(
            checkpoints,
            graph,
            routes,
            kind=self._agent.kind,
            texts=texts,
            on_run_end=self._run_ended,
            held_actions=self._actions_that_publish(),
            is_approved=None if gate is None else gate.is_approved,
            spending_actions=self._spending_actions(),
            is_cleared_to_spend=None if budget is None else budget.is_cleared,
            run_routes=run_routes,
        )
        gate_activities = [] if gate is None else [gate.propose_action, gate.settle_proposal]
        budget_activities = [] if budget is None else [budget.reserve_spend, budget.settle_spend]
        return AgentRegistration(
            task_queue=self._task_queue,
            workflows=list(workflows),
            activities=[
                runtime.run_graph_step,
                runtime.run_action,
                runtime.prepare_run,
                runtime.forget_run,
                *gate_activities,
                *budget_activities,
                *activities,
            ],
            checkpoint_cleanups=[
                *checkpoint_cleanups,
                stale_threads_of(self._agent.kind, longest_run),
            ],
            periodic_tasks=list(periodic_tasks),
        )

    def _require_a_run_that_outlasts_its_waits(self, longest_run: timedelta) -> None:
        if longest_run <= timedelta(0):
            raise InvalidAgentWorker(
                f"the longest run of agent {self._agent.kind!r} must be longer than nothing"
            )
        for action in self._actions_that_may_wait():
            wait = timedelta(seconds=self._agent.policies[action].approval_wait_seconds)
            if longest_run < wait:
                raise InvalidAgentWorker(
                    f"a run of agent {self._agent.kind!r} can wait {wait} for a person to decide "
                    f"on action {action!r}, longer than its longest run of {longest_run}: "
                    "its checkpoints would be removed while it waits"
                )
        for action, target in self._agent.run_targets.items():
            # A run the action waits for is a child workflow, and the parent's workflow stays
            # open for it as long as it runs; the target declares how long that can be.
            if target.kind != RunKind.WAIT:
                continue
            wait = timedelta(seconds=target.longest_seconds)
            if longest_run < wait:
                raise InvalidAgentWorker(
                    f"a run of agent {self._agent.kind!r} can wait {wait} for the run of action "
                    f"{action!r}, longer than its longest run of {longest_run}: "
                    "its checkpoints would be removed while it waits"
                )

    def _actions_that_may_wait(self) -> list[str]:
        return [
            action
            for action, policy in self._agent.policies.items()
            if may_wait_for_a_person(policy)
        ]

    def _actions_that_publish(self) -> list[str]:
        spending = set(self._spending_actions())
        return [action for action in self._actions_that_may_wait() if action not in spending]

    def _spending_actions(self) -> list[str]:
        return [action for action, policy in self._agent.policies.items() if spends(policy)]

    def _gate_of(self, sessions: sessionmaker[Session] | None) -> ProposalActivities | None:
        held = self._actions_that_may_wait()
        if not held:
            return None
        if sessions is None:
            raise InvalidAgentWorker(
                f"action {held[0]!r} of agent {self._agent.kind!r} waits for a person's approval: "
                "give registration the database sessions its proposals are stored with"
            )
        return ProposalActivities(sessions)

    def _budget_of(
        self, sessions: sessionmaker[Session] | None, environ: Mapping[str, str] | None
    ) -> SpendActivities | None:
        spending = self._spending_actions()
        if not spending:
            return None
        without_estimate = [action for action in spending if action not in self._estimates]
        if without_estimate:
            raise InvalidAgentWorker(
                f"agent {self._agent.kind!r} declares "
                f"{', '.join(repr(name) for name in without_estimate)} that spend with no "
                "estimate: register one with @worker.estimate"
            )
        if sessions is None or environ is None:
            raise InvalidAgentWorker(
                f"action {spending[0]!r} of agent {self._agent.kind!r} spends: give registration "
                "the database sessions its ledger is kept with and the process's environment, "
                "where a cap of the creative budget may be set"
            )
        spending_catalog = ActionCatalog(
            definition for definition in self._agent.definitions if definition.name in spending
        )
        return SpendActivities(
            sessions,
            environ,
            kind=self._agent.kind,
            catalog=spending_catalog,
            estimates=dict(self._estimates),
        )

    def _definition_of(self, payload: type[BaseModel]) -> ActionDefinition:
        for definition in self._agent.definitions:
            if definition.payload is payload:
                return definition
        raise InvalidAgentWorker(
            f"model {payload.__name__} is not the payload of an action of agent "
            f"{self._agent.kind!r}: declare it with @agent.action"
        )

    def _routes(self) -> ActionRoutes:
        without_handler = [
            definition.name
            for definition in self._agent.definitions
            if definition.run is None and definition.name not in self._handlers
        ]
        if without_handler:
            raise InvalidAgentWorker(
                f"agent {self._agent.kind!r} declares "
                f"{', '.join(repr(name) for name in without_handler)} with no handler: "
                "register one with @worker.handler"
            )
        return ActionRoutes(
            ActionRoute(definition, self._handlers[definition.name])
            for definition in self._agent.definitions
            if definition.run is None
        )

    def _run_routes(self) -> RunRoutes:
        without_prepare = [
            definition.name
            for definition in self._agent.definitions
            if definition.run is not None and definition.name not in self._prepares
        ]
        if without_prepare:
            raise InvalidAgentWorker(
                f"agent {self._agent.kind!r} declares "
                f"{', '.join(repr(name) for name in without_prepare)} that start another run "
                "with no prepare callable: register one with @worker.prepare_run"
            )
        return RunRoutes(
            RunRoute(definition, definition.run, self._prepares[definition.name])
            for definition in self._agent.definitions
            if definition.run is not None
        )

    def _require_a_workflow_of_this_agent(self, candidate: type) -> None:
        name = getattr(candidate, "__name__", repr(candidate))
        made_for = agent_of(candidate)
        if made_for is None:
            raise InvalidAgentWorker(
                f"{name} is not the workflow of an agent: make it one with @agent_workflow"
            )
        if made_for is not self._agent:
            raise InvalidAgentWorker(
                f"workflow {name} is the workflow of agent {made_for.kind!r}, "
                f"and this is the worker of agent {self._agent.kind!r}"
            )


def _named(function: Callable[..., Any]) -> str:
    return getattr(function, "__qualname__", repr(function))
