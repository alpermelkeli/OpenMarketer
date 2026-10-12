"""An agent's declared parts as a LangGraph graph, and the steps of its runs.

``compile_agent`` turns what an ``Agent`` declared (its nodes, where it starts,
its edges and routes) into a graph. It maps the parts one to one and decides
nothing: what makes a graph valid is ``Agent.shape``, which is framework-free
and raises ``InvalidAgent``, and what the library itself refuses is raised as the
same error. Each node and each route's chooser is handed to the library inside a
function of ours, which gives it the run's context (``Runtime.context``, which the
library does not store with the state) and keeps the library's types out of the module
of whoever wrote the node.

``AgentSteps`` is what the agent's wiring hands the worker as its graph: it
takes one step of a run on the run's thread (``GraphThread``), compiling the agent
with that run's checkpoints, and answers with a ``StepOutcome``. It structurally
satisfies the worker's ``AgentGraph`` protocol, which this module does not import.

What ``AgentSteps`` finishes with is what enters the workflow's history, which is
stored for as long as the namespace keeps it and is limited (the runtime refuses a
step outcome over 32 KiB). So the agent says, with ``finish``, what the workflow and
the run's reader need of the final state, and never the state: a model's whole
conversation is in it.

``run_from`` runs the same graph in memory from one of its nodes, for a command line and a
benchmark that have no workflow: it never pauses, so it cannot run an action.

This is one of four places that import LangGraph (see ``langgraph_steps.py``). It
knows no workflow engine and runs no action: a node asks for one with
``request_action``, and the workflow answers.
"""

from collections.abc import Callable, Mapping
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph
from langgraph.runtime import Runtime

from openmarketer_core.agent_runtime.actions import ActionOutcome
from openmarketer_core.agent_runtime.agent import Agent, GraphShape, InvalidAgent, Node, Route
from openmarketer_core.agent_runtime.langgraph_steps import CompiledGraph, GraphThread
from openmarketer_core.agent_runtime.steps import GraphRunRef, RunOfAnotherKind, StepOutcome
from openmarketer_core.graph_checkpoints import RunCheckpoints


class ActionAskedInLocalRun(Exception):
    """A node asked for an action in a run that has no workflow to run it."""

    def __init__(self, action_name: str) -> None:
        # The name is the one the payload's model was declared with, which is code; nothing of
        # the payload, which may hold what a model wrote.
        super().__init__(
            f"a node asked for action {action_name!r} in a run that never pauses: "
            "only a run in a workflow can have an action run"
        )
        self.action_name = action_name


def compile_agent(agent: Agent, checkpointer: BaseCheckpointSaver[str] | None) -> CompiledGraph:
    """The graph of ``agent``, compiled to save its checkpoints in ``checkpointer`` (if any).

    Raises ``InvalidAgent`` when the agent's parts do not make a graph, as ``Agent.shape``
    says, and when the library refuses the graph (a node named like a key of the state, say).
    """
    shape = agent.shape()
    return _compile(shape, checkpointer, shape.start)


def run_from(
    agent: Agent,
    node: str,
    state: Mapping[str, Any],
    *,
    recursion_limit: int,
    context: Any = None,
) -> dict[str, Any]:
    """Run the graph of ``agent`` in memory, from the node ``node`` on ``state``, to its end.

    For a run with no workflow, no checkpoints and nothing to wait for, as the command line's
    and the benchmark's, whose repository is already cloned: it is the graph the workflow runs,
    started at the node that follows the nodes that need one. It never pauses, so a node that
    asks for an action raises ``ActionAskedInLocalRun``, and nothing is stored: a run that
    fails is run again from the start. ``context`` is what a node of an agent with a context
    takes, and ``recursion_limit`` bounds the graph steps, as ``GraphThread`` says.

    Raises ``InvalidAgent`` for a ``node`` the agent does not have and for a context given to
    an agent that declared none, or not given to one that did.
    """
    shape = agent.shape()
    if node not in shape.nodes:
        raise InvalidAgent(f"agent {agent.kind!r} has no node {node!r} to run from")
    if (context is None) != (shape.context is None):
        raise InvalidAgent(_context_mismatch(agent.kind, shape.context))
    graph = _compile(shape, None, entry=node)
    output = graph.invoke(
        dict(state), {"recursion_limit": recursion_limit}, context=context, version="v2"
    )
    if output.interrupts:
        asked = output.interrupts[0].value
        name = asked.get("name") if isinstance(asked, dict) else None
        raise ActionAskedInLocalRun(name if isinstance(name, str) else "?")
    return dict(output.value)


class AgentSteps:
    """The steps of an agent's runs: ``advance`` is what the worker's ``AgentGraph`` asks for."""

    def __init__(
        self,
        agent: Agent,
        *,
        initial_state: Callable[[GraphRunRef], Mapping[str, Any]],
        finish: Callable[[GraphRunRef, Mapping[str, Any]], dict[str, Any]],
        recursion_limit: int,
        context: Callable[[GraphRunRef, RunCheckpoints], Any] | None = None,
    ) -> None:
        """What a run starts from, how it ends, and what its nodes are given besides the state.

        ``initial_state`` makes the state a run starts from, from the run's identifiers; it
        is called only for a run that has no checkpoint yet, as it may read a database.
        ``finish`` is called inside the step that ends a run, with the run and its final
        state: it may store what the run produced, so it is safe to repeat (the step can be
        repeated), and it answers with the small projection that enters the workflow history
        and is never the state. ``context`` makes, for each attempt, what the nodes take
        as a second parameter: it is given only by an agent that declared a context type.
        ``recursion_limit`` bounds the graph steps of one ``advance`` (``GraphThread`` says why
        it has no default), and none of the others has a default either: an agent decides what
        its runs start from and report.

        The graph is taken from the agent and compiled once here, so a defect is found when
        the worker starts and not at the first run, and what the agent declares afterwards
        does not reach the graph of a worker that is running. Raises ``InvalidAgent`` when
        the agent's parts do not make a graph, when the library refuses it, and when
        ``context`` is given to an agent that declared no context type or left out for one that
        did.
        """
        shape = agent.shape()
        if (context is None) != (shape.context is None):
            raise InvalidAgent(_context_mismatch(agent.kind, shape.context))
        _compile(shape, InMemorySaver(), shape.start)
        self._kind = agent.kind
        self._shape = shape
        self._initial_state = initial_state
        self._finish = finish
        self._context = context
        self._recursion_limit = recursion_limit

    def advance(
        self, run: GraphRunRef, checkpoints: RunCheckpoints, resume: ActionOutcome | None
    ) -> StepOutcome:
        """Take one step of ``run`` on its thread: ``GraphThread.advance`` says how.

        The step waits for the action a node asked for, or finishes with ``finish`` of the
        final state. The context is made once for the step. Raises ``RunOfAnotherKind`` for a
        run that is not of the agent's kind.
        """
        if run.kind != self._kind:
            raise RunOfAnotherKind(run.kind, self._kind)
        thread = GraphThread(
            _compile(self._shape, checkpoints.store, self._shape.start),
            checkpoints,
            recursion_limit=self._recursion_limit,
            context=None if self._context is None else self._context(run, checkpoints),
        )
        step = thread.advance(initial=lambda: self._initial_state(run), resume=resume)
        if step.output is None:
            return step
        return StepOutcome.finished(self._finish(run, step.output))


def _compile(
    shape: GraphShape, checkpointer: BaseCheckpointSaver[str] | None, entry: str
) -> CompiledGraph:
    """The graph of ``shape``, entered at the node ``entry``."""
    try:
        builder = StateGraph(shape.state, context_schema=shape.context)
        for node in shape.nodes.values():
            builder.add_node(node.name, _node_function(node))  # pyright: ignore[reportArgumentType]
        builder.add_edge(START, entry)
        for edge in shape.edges:
            # ``agent.END`` is LangGraph's own, so the end of an edge needs no translation.
            builder.add_edge(edge.after, edge.then)
        for route in shape.routes:
            builder.add_conditional_edges(route.after, _route_function(route), list(route.targets))
        return builder.compile(checkpointer=checkpointer)
    except ValueError as refused:
        # The library's messages name nodes and routes, which are the agent's own words.
        raise InvalidAgent(f"LangGraph refuses the graph of the agent: {refused}") from None


def _node_function(node: Node) -> Callable[..., Mapping[str, Any]]:
    # The library calls a function that has a parameter named ``runtime`` with the run's.
    def run_node(state: Any, runtime: Runtime[Any]) -> Mapping[str, Any]:
        return node.run(state, runtime.context)

    run_node.__name__ = node.name
    return run_node


def _route_function(route: Route) -> Callable[..., str]:
    def choose(state: Any, runtime: Runtime[Any]) -> str:
        return route.choose(state, runtime.context)

    choose.__name__ = getattr(route.chooser, "__name__", "route")
    return choose


def _context_mismatch(kind: str, declared: type | None) -> str:
    if declared is None:
        return f"agent {kind!r} declared no context type, and a context was given for its run"
    return f"agent {kind!r} declared the context type {declared.__name__}, and none was given"
