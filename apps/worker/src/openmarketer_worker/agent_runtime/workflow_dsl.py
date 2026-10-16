"""``agent_workflow``: an agent's workflow, written as a class that holds only what is its own.

Every agent's workflow does the same around its own steps: refuse a run that is not the agent's,
run the graph with the actions the agent declared, say how a run that did not finish ended, and
forget the run's checkpoints whatever happened. ``agent_workflow`` writes that, so the author's
class is what is particular to the agent and nothing else. It may define three optional hooks,
all ``async def``, each given the workflow's input as the author typed it (``run`` below):

- ``before(self, run)``: first. For marking the run started. It may return a ``GraphRunPolicy``,
  which the run's steps are then taken with instead of ``graph``: workflow code cannot read the
  environment, so limits that come from the worker's settings reach a run through the activity
  that ``before`` calls.
- ``after(self, run, output)``: once the graph finished, with the ``result`` its agent reports
  (``AgentSteps``). For storing what the run produced.
- ``failed(self, run, reason)``: when the run did not finish, with the sentence that may be
  shown to whoever asked for it. For recording why. It is called for a failure of ``before``,
  of the graph or of ``after``, and when the workflow was cancelled. It is not called for a run
  that finished, nor for a run that is not the agent's.

Then, in every case, the run's checkpoints are forgotten with the ``forget_run`` activity, which
tells the agent that the run ended. A failure of that activity is logged and does not replace
the run's own outcome: the rows are left to a cleanup. If ``failed`` itself fails, the checkpoints
are still forgotten and the workflow ends with the hook's failure, not the run's.

Once the run is over, nothing that follows can change how it ended, so ``failed`` and the
forgetting are waited for to their end even when the workflow is cancelled while they run: the
record is written, and the workflow ends as the run did (cancelled only when the run was, a
failure when it failed, its result when it finished), never as cancelled for a cancellation that
came too late. Both run as tasks of their own for that reason, and are not shielded: a shield
gives a cancellation to its caller at once and leaves the work behind.

A workflow can be started with any kind and run id by whoever reaches the server, and a
run's checkpoints and record are another agent's property: the first thing the workflow does is
to refuse a run whose kind is not the agent's (``RUN_OF_ANOTHER_KIND``, for good). It runs no
hook, takes no step and forgets nothing for it.

The class does not define ``run``: the decorator does, and refuses a class that does, so that no
workflow of an agent can start a run without the actions it declared, the hooks and the
forgetting. By default it takes a ``GraphRunRef``; an agent whose starter already has an input of
its own (the API's, shared with the worker) gives that class as ``input`` and says how to get
the run from it with ``run_of``. The annotation of ``run`` is that class itself, a real type and
never its name, because Temporal decodes the input from it.

The hooks are workflow code, which the sandbox loads and replays: deterministic, with no
network, file, clock, randomness or environment, and every side effect an activity. The
activities they call word their failures (``activity_support.worded_failures``), as the
runtime's own do. The reason ``failed`` gets is the message of the failure that ended the run
only when its type is in ``worded_types`` (the runtime's own, ``WORDED_BY_THE_RUNTIME``, to
which an agent adds the types its activities and handlers word); any other failure is told in
the fixed words of ``texts``, so that a message written for a log (a database driver's, with the
host it could not reach; the worker's library's) never reaches a run's record. The same
``worded_types`` decide what the graph is told of a failed action (``driver.py``).

``graph`` (a ``GraphRunPolicy``) and the agent's action policies are constants of the agent's
modules, never a workflow's input: they validate their values when they are made, and a
``ValueError`` while Temporal decodes a workflow's input fails the workflow task, which is
tried again for ever. A policy ``before`` returns is made in the workflow, from small values of
an activity's result, and is checked when it is made as well. What the driver refuses (an
action the agent did not declare, too many actions) is an ``ApplicationError`` of the workflow,
not of an activity, and ends the run like any other failure: ``failed`` hears of it.

``name`` is how the workflow is started (``client.start_workflow(name, run, ...)``), and what the
API and the worker share. The workflow id and the task queue are the starter's, and are decided
here by nobody. The class must be defined at the top of a module that the sandbox can load, so
it reaches the agent's declarations (``definitions.py``) and never the graph or the database.
Nothing here records anything on a run, holds an agent's words or reads an agent's settings:
that is the hooks' and the agent's.

An agent with an action the policy gate holds for a person (``ActionEffect.PUBLISHES`` or
``SPENDS``), or with an action that starts another agent's run (``Agent.action(run=...)``),
gives ``scope_of``, which says from the input which workspace and project a run works for: its
proposals and its child runs belong there. The decorator refuses such an agent's workflow
without it. Every workflow it makes hears the signal ``PROPOSAL_DECIDED_SIGNAL`` (the method
``proposal_decided``, which it writes as it writes ``run``), so that the API can wake a run that
waits for a person (``approval_wait.py``); the signal carries an id, never the decision.

The class is marked with the agent it was made for (``agent_of``), so that the worker's
registration refuses the workflow of another agent. Temporal accepts a ``run`` method only when
its qualified name is that of a method of the decorated class, and the decorator builds ``run``
itself: so it gives the function that name (``test_workflow_dsl.py`` pins both checks of
Temporal this relies on).
"""

import asyncio
import inspect
import uuid
from collections.abc import Callable, Collection
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError, FailureError

with workflow.unsafe.imports_passed_through():
    from openmarketer_core.agent_runtime.agent import Agent
    from openmarketer_core.agent_runtime.gate import (
        PROPOSAL_DECIDED_SIGNAL,
        GateVerdict,
        RunScope,
        verdict_for,
    )
    from openmarketer_core.agent_runtime.model_input import shown
    from openmarketer_core.agent_runtime.steps import GraphRunRef
    from openmarketer_worker.agent_runtime.approval_wait import DecidedProposals
    from openmarketer_worker.agent_runtime.driver import run_graph_with_actions
    from openmarketer_worker.agent_runtime.vocabulary import (
        FORGET_RUN,
        FORGET_RUN_LIMITS,
        RUN_OF_ANOTHER_KIND,
        RUN_TEXTS,
        STEP_REFUSED,
        WORDED_BY_THE_RUNTIME,
        FailureTexts,
        GraphRunPolicy,
    )
    from openmarketer_worker.agent_runtime.workflow_support import (
        database_write,
        reason_shown,
        until_it_ends,
    )

# The hooks a class may define, with what each is called with, in order.
_HOOKS: dict[str, tuple[str, ...]] = {
    "before": ("run",),
    "after": ("run", "output"),
    "failed": ("run", "reason"),
}

# What ends a run and so is told to ``failed``: every failure Temporal ends a workflow with (an
# activity that failed, the driver's own refusals, a child workflow or a timeout of a hook's)
# and a cancellation. Anything else is a defect of the workflow's code, which fails the
# workflow task and is tried again once the code is fixed: that run is not over.
_RUN_FAILURES = (FailureError, asyncio.CancelledError)

# Where a workflow class keeps the agent it was made for. A dunder cannot clash with a hook.
_AGENT_OF_THE_WORKFLOW = "__agent_of_the_workflow__"
# Where a workflow instance keeps the decisions it heard of, and the method that hears them.
_DECIDED_PROPOSALS = "__decided_proposals__"
_SIGNAL_METHOD = "proposal_decided"


class InvalidAgentWorkflow(ValueError):
    """A class cannot be the workflow of an agent, or it is declared without a way to start it."""


def agent_of(workflow_class: type) -> Agent | None:
    """The agent ``workflow_class`` was made the workflow of by ``agent_workflow``, if it was.

    Only the class that was decorated carries the mark, not a subclass of it.
    """
    return vars(workflow_class).get(_AGENT_OF_THE_WORKFLOW)


def agent_workflow[Workflow](
    agent: Agent,
    *,
    name: str,
    graph: GraphRunPolicy,
    input: type = GraphRunRef,
    run_of: Callable[[Any], GraphRunRef] | None = None,
    scope_of: Callable[[Any], RunScope] | None = None,
    texts: FailureTexts = RUN_TEXTS,
    worded_types: Collection[str] = WORDED_BY_THE_RUNTIME,
) -> Callable[[type[Workflow]], type[Workflow]]:
    """Make the class it decorates the Temporal workflow ``name`` that runs ``agent``.

    The class gets a ``run(self, run_input: input) -> dict[str, Any]``: the output is what the
    agent's graph finished with. ``input`` is the class the workflow is started with and
    ``run_of`` turns an input of it into the run (a ``GraphRunRef``); both are left out by a
    workflow that is started with the ``GraphRunRef`` itself. ``graph`` is the default limits
    of the run's steps, which ``before`` may replace. ``texts`` and ``worded_types`` say how a
    failure is told to ``failed``, as the module's docstring says. ``scope_of`` turns an input
    into the workspace and project of the run, for an agent with an action the gate holds.

    Raises ``InvalidAgentWorkflow`` when the class defines ``run`` or a hook that is not an
    ``async def`` taking what its hook is given, or is not defined at the top of a module, when
    ``name`` is empty, when ``input`` is not a class, when ``run_of`` is given without an
    ``input`` or ``input`` without ``run_of``, when ``worded_types`` is one text and not
    a collection of them, and when the agent has an action the gate holds and no ``scope_of``.
    """
    if not name:
        raise InvalidAgentWorkflow(
            "an agent's workflow needs a name: it is how the workflow is started"
        )
    if not isinstance(input, type):
        raise InvalidAgentWorkflow(
            f"the input of workflow {name!r} is a class and not {input!r}: "
            "Temporal decodes the input from the class itself, never from its name"
        )
    if isinstance(worded_types, str):
        raise InvalidAgentWorkflow(
            f"worded_types of workflow {name!r} is a collection of failure types, not one text"
        )
    derive_run = _way_to_get_the_run(name, input, run_of)
    _require_a_scope_for_actions_that_need_one(name, agent, scope_of)
    worded = frozenset(worded_types)

    def decorate(cls: type[Workflow]) -> type[Workflow]:
        _require_a_class_that_can_be_a_workflow(cls)

        async def run(self, run_input):  # annotated below
            run_ref = derive_run(run_input)
            _require_a_run_of(agent, run_ref)
            return await _run_agent(
                self,
                run_input,
                run_ref,
                agent=agent,
                graph=graph,
                texts=texts,
                worded_types=worded,
                scope=None if scope_of is None else scope_of(run_input),
            )

        def proposal_decided(self, proposal_id: uuid.UUID) -> None:
            _decided_proposals_of(self).hear(proposal_id)

        # Temporal reads the input's type from the annotation, and a real type is what it needs.
        run.__annotations__ = {"run_input": input, "return": dict[str, Any]}
        proposal_decided.__annotations__ = {"proposal_id": uuid.UUID, "return": None}
        # Temporal checks the qualified name and the module of a method against the class.
        for method in (run, proposal_decided):
            method.__qualname__ = f"{cls.__qualname__}.{method.__name__}"
            method.__module__ = cls.__module__
        # The class has neither for the type checker: that is the point of the decorator.
        cls.run = workflow.run(run)  # pyright: ignore[reportAttributeAccessIssue]
        signal = workflow.signal(name=PROPOSAL_DECIDED_SIGNAL)(proposal_decided)
        setattr(cls, _SIGNAL_METHOD, signal)
        decorated = workflow.defn(name=name)(cls)
        setattr(decorated, _AGENT_OF_THE_WORKFLOW, agent)
        return decorated

    return decorate


def _way_to_get_the_run(
    name: str, input: type, run_of: Callable[[Any], GraphRunRef] | None
) -> Callable[[Any], GraphRunRef]:
    if input is GraphRunRef:
        if run_of is not None:
            raise InvalidAgentWorkflow(
                f"workflow {name!r} takes the run itself, which needs no run_of: "
                "give input as well to take an input of its own"
            )
        return _the_run_itself
    if run_of is None:
        raise InvalidAgentWorkflow(
            f"workflow {name!r} takes {input.__name__} as its input: "
            "give run_of, which says how to get the run out of it"
        )
    return run_of


def _the_run_itself(run: GraphRunRef) -> GraphRunRef:
    return run


def _require_a_scope_for_actions_that_need_one(
    name: str, agent: Agent, scope_of: Callable[[Any], RunScope] | None
) -> None:
    """Refuse a workflow whose agent has an action that needs the run's workspace and project."""
    if scope_of is not None:
        return
    held = [
        action
        for action, policy in agent.policies.items()
        if verdict_for(policy) == GateVerdict.ASK_A_PERSON
    ]
    if held:
        raise InvalidAgentWorkflow(
            f"workflow {name!r} runs agent {agent.kind!r}, whose action {held[0]!r} waits for a "
            "person's approval: give scope_of, which says which project a run proposes it in"
        )
    starting = list(agent.run_targets)
    if starting:
        raise InvalidAgentWorkflow(
            f"workflow {name!r} runs agent {agent.kind!r}, whose action {starting[0]!r} starts "
            "another run, which belongs to the run's project: give scope_of, which says which "
            "workspace and project a run works for"
        )


def _decided_proposals_of(instance: object) -> DecidedProposals:
    # Made on first use, as a signal may come before ``run`` starts and the class has no
    # ``__init__`` of the decorator's.
    return vars(instance).setdefault(_DECIDED_PROPOSALS, DecidedProposals())


def _require_a_class_that_can_be_a_workflow(cls: type) -> None:
    if cls.__qualname__ != cls.__name__:
        raise InvalidAgentWorkflow(
            f"workflow class {cls.__name__} is defined inside a function or another class: "
            "Temporal reaches a workflow by the name of its module and class, so define it "
            "at the top of a module"
        )
    for written in ("run", _SIGNAL_METHOD):
        if hasattr(cls, written):
            raise InvalidAgentWorkflow(
                f"workflow class {cls.__name__} defines {written}, which agent_workflow writes: "
                "put what is particular to the agent in the hooks before, after and failed"
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


def _require_a_run_of(agent: Agent, run: GraphRunRef) -> None:
    if run.kind != agent.kind:
        # Quoted and short: the kind is whatever the workflow was started with.
        raise ApplicationError(
            f"a run of kind {shown(run.kind)} is not a run of agent {agent.kind!r}",
            type=RUN_OF_ANOTHER_KIND,
            non_retryable=True,
        )


async def _run_agent(
    instance: object,
    run_input: Any,
    run: GraphRunRef,
    *,
    agent: Agent,
    graph: GraphRunPolicy,
    texts: FailureTexts,
    worded_types: Collection[str],
    scope: RunScope | None,
) -> dict[str, Any]:
    try:
        policy = await _before(instance, run_input, graph)
        output = await run_graph_with_actions(
            run,
            actions=agent.policies,
            policy=policy,
            worded_types=worded_types,
            scope=scope,
            decided=_decided_proposals_of(instance),
            run_targets=agent.run_targets,
        )
        await _call_hook(instance, "after", run_input, output)
    except _RUN_FAILURES as failure:
        try:
            failed = getattr(instance, "failed", None)
            if failed is not None:
                reason = reason_shown(failure, texts, worded_types=worded_types)
                await until_it_ends(failed(run_input, reason))
        finally:
            await _forget(run)
        raise
    await _forget(run)
    return output


async def _before(instance: object, run_input: Any, default: GraphRunPolicy) -> GraphRunPolicy:
    defined = getattr(instance, "before", None)
    if defined is None:
        return default
    chosen = await defined(run_input)
    return default if chosen is None else chosen


async def _call_hook(instance: object, hook: str, *arguments: Any) -> None:
    defined = getattr(instance, hook, None)
    if defined is not None:
        await defined(*arguments)


async def _forget(run: GraphRunRef) -> None:
    try:
        await until_it_ends(
            database_write(FORGET_RUN, run, FORGET_RUN_LIMITS, not_retried=[STEP_REFUSED])
        )
    except ActivityError as failure:
        # The outcome of the run is what its starter reads; rows left behind are not worth
        # replacing it.
        workflow.logger.warning(
            "the checkpoints of %s run %s were not forgotten: %s",
            shown(run.kind),
            run.run_id,
            reason_shown(failure, RUN_TEXTS, worded_types=WORDED_BY_THE_RUNTIME),
        )
