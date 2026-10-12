"""The graphs the agent runtime's tests run, built with core's ``GraphThread`` and no model.

``ScriptedGraph`` asks for the actions of a plan one after another, through core's
``request_action`` on the real checkpoint store, and stops at the first action that failed: that
is "the agent's own decision" in these tests. It remembers how it was advanced, and can die once
right after an outcome was applied, as a worker does. The other graphs fail, block or ask
for what a rule of core refuses, and are not LangGraph graphs at all unless they say so.
"""

import operator
import threading
import time
from dataclasses import asdict, dataclass
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, create_model

from openmarketer_core.agent_runtime.actions import ActionOutcome, mark_as_action
from openmarketer_core.agent_runtime.agent import Agent
from openmarketer_core.agent_runtime.langgraph_agent import AgentSteps
from openmarketer_core.agent_runtime.langgraph_steps import (
    CompiledGraph,
    GraphThread,
    request_action,
)
from openmarketer_core.agent_runtime.steps import GraphRunRef, StepOutcome
from openmarketer_core.graph_checkpoints import RunCheckpoints

# Graph steps of one advance: far more than a plan of a few actions takes.
GRAPH_STEPS = 100


def asking_for(name: str, payload: dict[str, Any]) -> BaseModel:
    """What the graph asks with: any payload for the action ``name``, so that a test can ask for
    what no model fits."""
    anything = create_model("Anything", __config__=ConfigDict(extra="allow"))
    mark_as_action(anything, name)
    return anything(**payload)


@dataclass(frozen=True)
class Ask:
    """One action of a graph's plan."""

    name: str
    payload: dict[str, Any]


SEND_THEN_PUBLISH = [Ask("send_email", {"to": "ada@example.com"}), Ask("publish", {"post": 7})]


class State(TypedDict):
    plan: list[dict[str, Any]]
    answers: Annotated[list[dict[str, Any]], operator.add]
    note: Any  # whatever a test makes the state, and so the final output, as big as it wants


def asks_for_the_next_action(state: State) -> dict[str, Any]:
    ask = state["plan"][len(state["answers"])]
    outcome = request_action(asking_for(ask["name"], ask["payload"]))
    return {"answers": [asdict(outcome)]}


def asks_twice(state: State) -> dict[str, Any]:
    """A node that breaks the rule of one action per node: it asks again once answered."""
    first = request_action(asking_for("send_email", {"to": "ada@example.com"}))
    second = request_action(asking_for("publish", {"post": 7}))
    return {"answers": [asdict(first), asdict(second)]}


class Unregistered(BaseModel):
    """A payload model that no agent declared as an action."""

    model_config = ConfigDict(extra="forbid")

    to: str


def asks_with_an_unregistered_model(state: State) -> dict[str, Any]:
    request_action(Unregistered(to="ada@example.com"))
    return {"answers": []}


def goes_on(state: State) -> str:
    answers = state["answers"]
    if answers and not answers[-1]["succeeded"]:
        return END
    return "ask" if len(answers) < len(state["plan"]) else END


def compiled(checkpoints: RunCheckpoints, node: Any = asks_for_the_next_action) -> CompiledGraph:
    builder = StateGraph(State)
    builder.add_node("ask", node)
    builder.add_conditional_edges(START, goes_on, ["ask", END])
    builder.add_conditional_edges("ask", goes_on, ["ask", END])
    return builder.compile(checkpointer=checkpoints.store)


class ScriptedGraph:
    """An ``AgentGraph`` that asks for the actions of ``plan`` and remembers how it was advanced.

    ``note`` is part of the state it starts with, and so of the output it finishes with.
    ``dies_after_applying`` is how many times a step dies right after it applied an outcome.
    ``asking_twice`` swaps the node for one that asks for a second action in one run.
    """

    def __init__(
        self,
        plan: list[Ask],
        *,
        note: Any = None,
        dies_after_applying: int = 0,
        asking_twice: bool = False,
    ) -> None:
        self._plan = plan
        self._note = note
        self._deaths_left = dies_after_applying
        self._node = asks_twice if asking_twice else asks_for_the_next_action
        self.advanced: list[ActionOutcome | None] = []  # the outcome each step was given

    def advance(
        self, run: GraphRunRef, checkpoints: RunCheckpoints, resume: ActionOutcome | None
    ) -> StepOutcome:
        self.advanced.append(resume)
        initial: dict[str, Any] = {"plan": [asdict(ask) for ask in self._plan], "answers": []}
        if self._note is not None:
            initial["note"] = self._note
        graph = compiled(checkpoints, self._node)
        thread = GraphThread(graph, checkpoints, recursion_limit=GRAPH_STEPS)
        step = thread.advance(initial=initial, resume=resume)
        if resume is not None and self._deaths_left:
            self._deaths_left -= 1
            # The outcome is applied and stored; what the step found out is lost with the worker.
            raise RuntimeError("the worker died after the outcome was applied")
        return step


class GraphOfNode:
    """An ``AgentGraph`` whose one node is ``node``, started with a plan of one action."""

    def __init__(self, node: Any) -> None:
        self._node = node

    def advance(
        self, run: GraphRunRef, checkpoints: RunCheckpoints, resume: ActionOutcome | None
    ) -> StepOutcome:
        initial = {"plan": [asdict(SEND_THEN_PUBLISH[0])], "answers": []}
        graph = compiled(checkpoints, self._node)
        thread = GraphThread(graph, checkpoints, recursion_limit=GRAPH_STEPS)
        return thread.advance(initial=initial, resume=resume)


class NoteState(TypedDict):
    note: str


def steps_of_an_agent_whose_route_chooses_outside_its_targets() -> AgentSteps:
    """The graph of a declared agent whose route sends the run to a node it was not given."""
    agent = Agent("scripted-route", state=NoteState)

    @agent.node(start=True)
    def first(state: NoteState) -> dict[str, Any]:
        return {"note": "first"}

    @agent.node
    def second(state: NoteState) -> dict[str, Any]:
        return {"note": "second"}

    @agent.route(after=first, to=[second])
    def chooses_wrongly(state: NoteState) -> str:
        return "elsewhere"

    return AgentSteps(
        agent,
        initial_state=lambda run: {"note": ""},
        finish=lambda run, final: dict(final),
        recursion_limit=GRAPH_STEPS,
    )


class RaisingGraph:
    """An ``AgentGraph`` that fails with ``error`` whenever it is advanced."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.advanced = 0

    def advance(
        self, run: GraphRunRef, checkpoints: RunCheckpoints, resume: ActionOutcome | None
    ) -> StepOutcome:
        self.advanced += 1
        raise self._error


class BlockingGraph:
    """An ``AgentGraph`` whose step does not return until it is released: a graph that works."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def advance(
        self, run: GraphRunRef, checkpoints: RunCheckpoints, resume: ActionOutcome | None
    ) -> StepOutcome:
        self.started.set()
        self.release.wait(timeout=60)
        return StepOutcome.finished({})


class SlowGraph:
    """An ``AgentGraph`` whose step works for ``seconds`` and then finishes."""

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds

    def advance(
        self, run: GraphRunRef, checkpoints: RunCheckpoints, resume: ActionOutcome | None
    ) -> StepOutcome:
        time.sleep(self._seconds)
        return StepOutcome.finished({})


class ReturningGraph:
    """An ``AgentGraph`` that answers every step with ``outcome``, whatever it holds."""

    def __init__(self, outcome: StepOutcome) -> None:
        self._outcome = outcome

    def advance(
        self, run: GraphRunRef, checkpoints: RunCheckpoints, resume: ActionOutcome | None
    ) -> StepOutcome:
        return self._outcome
