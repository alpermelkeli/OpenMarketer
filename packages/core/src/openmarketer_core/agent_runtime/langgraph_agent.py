"""An agent's declared parts as a LangGraph graph, and the steps of its runs.

``compile_agent`` turns what an ``Agent`` declared (its nodes, where it starts,
its edges and routes) into a graph. It maps the parts one to one and decides
nothing: what makes a graph valid is ``Agent.shape``, which is framework-free
and raises ``InvalidAgent``.

``AgentSteps`` is what the agent's wiring hands the worker as its graph: it
takes one step of a run on the run's thread (``GraphThread``), compiling the agent
with that run's checkpoints, and answers with a ``StepOutcome``. It structurally
satisfies the worker's ``AgentGraph`` protocol, which this module does not import.

What ``AgentSteps`` finishes with is what enters the workflow's history, which is
stored for as long as the namespace keeps it and is limited (the runtime refuses a
step outcome over 32 KiB). So the agent says, with ``result``, what the workflow and
the run's reader need of the final state, and never the state: a model's whole
conversation is in it.

This is one of four places that import LangGraph (see ``langgraph_steps.py``). It
knows no workflow engine and runs no action: a node asks for one with
``request_action``, and the workflow answers.
"""

from collections.abc import Callable, Mapping
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START, StateGraph

from openmarketer_core.agent_runtime.actions import ActionOutcome
from openmarketer_core.agent_runtime.agent import Agent
from openmarketer_core.agent_runtime.langgraph_steps import CompiledGraph, GraphThread
from openmarketer_core.agent_runtime.steps import GraphRunRef, RunOfAnotherKind, StepOutcome
from openmarketer_core.graph_checkpoints import RunCheckpoints


def compile_agent(agent: Agent, checkpointer: BaseCheckpointSaver[str]) -> CompiledGraph:
    """The graph of ``agent``, compiled to save its checkpoints in ``checkpointer``.

    Raises ``InvalidAgent`` when the agent's parts do not make a graph (``Agent.shape``).
    """
    shape = agent.shape()
    builder = StateGraph(agent.state)
    for name, function in shape.nodes.items():
        # The library types a node's parameter as a keyword named ``state``, and calls the node
        # with the state as its one positional argument.
        builder.add_node(name, function)  # pyright: ignore[reportArgumentType]
    builder.add_edge(START, shape.start)
    for edge in shape.edges:
        # ``agent.END`` is LangGraph's own, so the end of an edge needs no translation.
        builder.add_edge(edge.after, edge.then)
    for route in shape.routes:
        builder.add_conditional_edges(route.after, route.choose, list(route.targets))
    return builder.compile(checkpointer=checkpointer)


class AgentSteps:
    """The steps of an agent's runs: ``advance`` is what the worker's ``AgentGraph`` asks for."""

    def __init__(
        self,
        agent: Agent,
        *,
        initial_state: Callable[[GraphRunRef], Mapping[str, Any]],
        result: Callable[[Mapping[str, Any]], dict[str, Any]],
        recursion_limit: int,
    ) -> None:
        """``initial_state`` is the state a run starts from, made of the run's identifiers.

        ``result`` is the small projection of a run's final state that the step
        finishes with: it enters the workflow history, so it is never the whole
        state. ``recursion_limit`` bounds the graph steps of one ``advance``
        (``GraphThread`` says why it has no default), and neither has ``result``:
        an agent decides what its runs report.

        Raises ``InvalidAgent`` when the agent's parts do not make a graph, here
        and so when the worker starts, not at the first run.
        """
        agent.shape()
        self._agent = agent
        self._initial_state = initial_state
        self._result = result
        self._recursion_limit = recursion_limit

    def advance(
        self, run: GraphRunRef, checkpoints: RunCheckpoints, resume: ActionOutcome | None
    ) -> StepOutcome:
        """Take one step of ``run`` on its thread: ``GraphThread.advance`` says how.

        The step waits for the action a node asked for, or finishes with
        ``result`` of the final state. Raises ``RunOfAnotherKind`` for a run that is
        not of the agent's kind.
        """
        if run.kind != self._agent.kind:
            raise RunOfAnotherKind(run.kind, self._agent.kind)
        thread = GraphThread(
            compile_agent(self._agent, checkpoints.store),
            checkpoints,
            recursion_limit=self._recursion_limit,
        )
        step = thread.advance(initial=self._initial_state(run), resume=resume)
        if step.output is None:
            return step
        return StepOutcome.finished(self._result(step.output))
