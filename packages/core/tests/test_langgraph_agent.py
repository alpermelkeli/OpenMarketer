"""Tests for ``compile_agent``, ``AgentSteps`` and ``run_from``: an agent written with decorators,
run for real.

The agents here are made of nothing but decorators and scripted nodes, with no model. Most tests
keep their checkpoints in memory; the last ones use the PostgreSQL store (fixtures in the
``conftest.py`` at the repository root), which stores them with the strict serializer.
"""

import operator
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END as LANGGRAPH_END
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine

from openmarketer_core.agent_runtime.actions import ActionOutcome, ActionRequest, NotAnAction
from openmarketer_core.agent_runtime.agent import END, Agent, InvalidAgent, InvalidRouteChoice
from openmarketer_core.agent_runtime.langgraph_agent import (
    ActionAskedInLocalRun,
    AgentSteps,
    compile_agent,
    run_from,
)
from openmarketer_core.agent_runtime.langgraph_steps import request_action
from openmarketer_core.agent_runtime.steps import (
    ActionAskedByTool,
    GraphRunRef,
    InvalidNodeResult,
    RunOfAnotherKind,
    StepError,
    StepOutcome,
)
from openmarketer_core.agent_runtime.threads import thread_id
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.graph_checkpoints.postgres import run_checkpoints
from openmarketer_core.run_events import RunEvent

KIND = "newsletter"
SECRET = "sk-live-4242-do-not-repeat"
# Far more graph steps than any test agent takes in one advance.
RECURSION_LIMIT = 50


class State(TypedDict):
    recipient: str
    log: Annotated[list[str], operator.add]
    delivered: bool
    reply: dict[str, Any]


def initial_state(run: GraphRunRef) -> dict[str, Any]:
    return {"recipient": f"{run.run_id}@example.com", "log": []}


def projection(run: GraphRunRef, final: Mapping[str, Any]) -> dict[str, Any]:
    """What a run reports: the log and whether it was delivered, not the state."""
    return {"log": final["log"], "delivered": final["delivered"]}


def newsletter(runs: list[str], dies_once: list[bool] | None = None) -> Agent:
    """An agent of decorators only: draft, send an e-mail (an action), and apologise on failure."""
    agent = Agent(KIND, state=State)

    @agent.action("send_email", timeout=10, attempts=2, retry_after=1)
    class SendEmail(BaseModel):
        model_config = ConfigDict(extra="forbid")

        to: str

    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        runs.append("draft")
        return {"log": ["drafted"]}

    @agent.node
    def send(state: State) -> dict[str, Any]:
        runs.append("send")
        outcome = request_action(SendEmail(to=state["recipient"]))
        if dies_once:
            dies_once.pop()
            raise RuntimeError("the worker died")
        return {
            "log": ["sent" if outcome.succeeded else "refused"],
            "delivered": outcome.succeeded,
            "reply": outcome.result,
        }

    @agent.node
    def apologise(state: State) -> dict[str, Any]:
        runs.append("apologise")
        return {"log": ["apologised"]}

    agent.edge(draft, send)
    agent.edge(apologise, END)

    @agent.route(after=send, to=[apologise, END])
    def after_send(state: State) -> str:
        return END if state["delivered"] else "apologise"

    return agent


def steps_of(
    agent: Agent,
    finish: Callable[[GraphRunRef, Mapping[str, Any]], dict[str, Any]] = projection,
) -> AgentSteps:
    return AgentSteps(
        agent, initial_state=initial_state, finish=finish, recursion_limit=RECURSION_LIMIT
    )


def checkpoints_for(store: BaseCheckpointSaver[str], run: GraphRunRef) -> RunCheckpoints:
    return RunCheckpoints(store=store, thread_id=thread_id(run.kind, run.run_id))


def success(request: ActionRequest, **result: Any) -> ActionOutcome:
    return ActionOutcome(action_id=request.action_id, succeeded=True, result=result, error="")


def failure(request: ActionRequest, error: str) -> ActionOutcome:
    return ActionOutcome(action_id=request.action_id, succeeded=False, result={}, error=error)


def waiting_action(step: StepOutcome) -> ActionRequest:
    assert step.output is None and step.action is not None
    return step.action


@pytest.fixture
def store() -> InMemorySaver:
    return InMemorySaver()


@pytest.fixture
def run() -> GraphRunRef:
    return GraphRunRef(KIND, uuid.uuid4())


@pytest.fixture
def runs() -> list[str]:
    return []


# ---------------------------------------------------------- compiling an agent
def test_end_of_the_graph_is_the_text_langgraph_calls_its_own():
    # ``compile_agent`` passes the end of an edge to the library as it is.
    assert END == LANGGRAPH_END


def test_agent_is_compiled_without_checkpoints_when_it_is_given_no_store(runs):
    assert compile_agent(newsletter(runs), None).checkpointer is None


def test_agent_is_compiled_with_the_checkpoint_store_it_is_given(store, runs):
    graph = compile_agent(newsletter(runs), store)

    assert graph.checkpointer is store
    assert {"draft", "send", "apologise"} <= set(graph.get_graph().nodes)


def declared_nothing() -> Agent:
    return Agent(KIND, state=State)


def declared_no_start() -> Agent:
    agent = declared_nothing()

    @agent.node
    def draft(state: State) -> dict[str, Any]:
        return {}

    return agent


def declared_edge_to_a_stray() -> Agent:
    agent = declared_nothing()

    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    def stray(state: State) -> dict[str, Any]:
        return {}

    agent.edge(draft, stray)
    return agent


def declared_route_from_a_stray() -> Agent:
    agent = declared_nothing()

    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    def stray(state: State) -> dict[str, Any]:
        return {}

    agent.route(after=stray, to=[draft])(lambda state: "draft")
    return agent


@pytest.mark.parametrize(
    ("declare", "problem"),
    [
        (declared_nothing, "no nodes"),
        (declared_no_start, "no start node"),
        (declared_edge_to_a_stray, "'stray'.*not a node"),
        (declared_route_from_a_stray, "'stray'.*not a node"),
    ],
    ids=["no nodes", "no start", "edge to a stray", "route from a stray"],
)
def test_agent_whose_parts_do_not_make_a_graph_is_not_compiled(store, declare, problem):
    with pytest.raises(InvalidAgent, match=problem):
        compile_agent(declare(), store)


def test_agent_that_is_not_a_graph_is_refused_when_its_steps_are_made_not_at_its_first_run():
    with pytest.raises(InvalidAgent, match="no start node"):
        steps_of(declared_no_start())


def test_node_with_no_way_out_is_not_an_error(store):
    agent = declared_nothing()

    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {"log": ["drafted"]}

    graph = compile_agent(agent, store)

    assert graph.invoke({"log": []}, {"configurable": {"thread_id": "t"}})["log"] == ["drafted"]


# ------------------------------------------------------- pausing and resuming
def test_agent_pauses_at_the_action_a_node_asks_for(store, runs, run):
    step = steps_of(newsletter(runs)).advance(run, checkpoints_for(store, run), None)

    action = waiting_action(step)
    assert action.name == "send_email"
    assert action.payload == {"to": f"{run.run_id}@example.com"}
    assert action.action_id.startswith(f"{KIND}:{run.run_id}:")
    assert runs == ["draft", "send"]


def test_agent_finishes_with_what_finish_makes_of_its_state_and_not_the_state(store, runs, run):
    steps = steps_of(newsletter(runs))
    checkpoints = checkpoints_for(store, run)
    action = waiting_action(steps.advance(run, checkpoints, None))

    step = steps.advance(run, checkpoints, success(action, message_id="m-1"))

    assert step == StepOutcome.finished({"log": ["drafted", "sent"], "delivered": True})


def test_finish_is_given_the_run_and_the_whole_final_state(store, runs, run):
    seen: list[tuple[GraphRunRef, Mapping[str, Any]]] = []

    def finish(finished: GraphRunRef, final: Mapping[str, Any]) -> dict[str, Any]:
        seen.append((finished, final))
        return {}

    steps = steps_of(newsletter(runs), finish)
    checkpoints = checkpoints_for(store, run)
    action = waiting_action(steps.advance(run, checkpoints, None))

    steps.advance(run, checkpoints, success(action, message_id="m-1"))

    ((finished, final),) = seen
    assert finished == run
    assert final["reply"] == {"message_id": "m-1"}
    assert final["recipient"] == f"{run.run_id}@example.com"


def test_finish_is_not_called_while_the_agent_waits(store, runs, run):
    def finish(run: GraphRunRef, final: Mapping[str, Any]) -> dict[str, Any]:
        raise AssertionError("the run has not finished")

    steps_of(newsletter(runs), finish).advance(run, checkpoints_for(store, run), None)


def test_finish_is_called_again_by_a_step_repeated_on_a_run_that_finished(store, runs, run):
    # The step that finished a run may be repeated: what ``finish`` stores must survive that.
    finished_runs: list[GraphRunRef] = []

    def finish(finished: GraphRunRef, final: Mapping[str, Any]) -> dict[str, Any]:
        finished_runs.append(finished)
        return {"delivered": final["delivered"]}

    steps = steps_of(newsletter(runs), finish)
    checkpoints = checkpoints_for(store, run)
    action = waiting_action(steps.advance(run, checkpoints, None))
    outcome = success(action)
    first = steps.advance(run, checkpoints, outcome)

    assert steps.advance(run, checkpoints, outcome) == first
    assert finished_runs == [run, run]


def test_failed_action_reaches_the_route_which_sends_the_agent_to_another_node(store, runs, run):
    steps = steps_of(newsletter(runs))
    checkpoints = checkpoints_for(store, run)
    action = waiting_action(steps.advance(run, checkpoints, None))

    step = steps.advance(run, checkpoints, failure(action, "the mail server refused it"))

    assert step == StepOutcome.finished(
        {"log": ["drafted", "refused", "apologised"], "delivered": False}
    )


def test_attempt_repeated_on_a_paused_run_returns_the_same_action_and_runs_nothing(
    store, runs, run
):
    steps = steps_of(newsletter(runs))
    checkpoints = checkpoints_for(store, run)
    action = waiting_action(steps.advance(run, checkpoints, None))
    runs_before = list(runs)

    assert waiting_action(steps.advance(run, checkpoints, None)) == action
    assert runs == runs_before


def test_outcome_that_an_earlier_attempt_applied_is_not_applied_twice(store, runs, run):
    steps = steps_of(newsletter(runs))
    checkpoints = checkpoints_for(store, run)
    action = waiting_action(steps.advance(run, checkpoints, None))
    outcome = success(action, message_id="m-1")
    finished = steps.advance(run, checkpoints, outcome)
    runs_before = list(runs)

    assert steps.advance(run, checkpoints, outcome) == finished
    assert runs == runs_before


def test_node_that_died_after_it_was_answered_is_given_the_same_answer_by_the_next_attempt(
    store, runs, run
):
    steps = steps_of(newsletter(runs, dies_once=[True]))
    checkpoints = checkpoints_for(store, run)
    action = waiting_action(steps.advance(run, checkpoints, None))
    outcome = success(action, message_id="m-1")
    with pytest.raises(RuntimeError, match="worker died"):
        steps.advance(run, checkpoints, outcome)

    step = steps.advance(run, checkpoints, outcome)

    assert step == StepOutcome.finished({"log": ["drafted", "sent"], "delivered": True})


def test_run_of_another_kind_is_refused_and_nothing_runs(store, runs):
    other = GraphRunRef("another-kind", uuid.uuid4())

    with pytest.raises(RunOfAnotherKind, match="another-kind.*newsletter"):
        steps_of(newsletter(runs)).advance(other, checkpoints_for(store, other), None)

    assert runs == []
    assert (
        store.get_tuple({"configurable": {"thread_id": thread_id(other.kind, other.run_id)}})
        is None
    )


def test_route_that_chooses_what_is_not_one_of_its_targets_stops_the_run(store, run):
    agent = Agent(KIND, state=State)

    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {"log": ["drafted"]}

    @agent.route(after=draft, to=[END])
    def after_draft(state: State) -> str:
        return "somewhere-else"

    with pytest.raises(InvalidRouteChoice, match="after node 'draft'"):
        steps_of(agent).advance(run, checkpoints_for(store, run), None)


def test_graph_steps_of_one_advance_are_bounded_by_the_limit_the_agent_gives(store, run):
    agent = Agent(KIND, state=State)

    @agent.node(start=True)
    def tick(state: State) -> dict[str, Any]:
        return {"log": ["tick"]}

    @agent.route(after=tick, to=[tick, END])
    def keep_going(state: State) -> str:
        return END if len(state["log"]) >= 10 else "tick"

    limited = AgentSteps(agent, initial_state=initial_state, finish=projection, recursion_limit=5)
    with pytest.raises(GraphRecursionError):
        limited.advance(run, checkpoints_for(store, run), None)

    roomy = AgentSteps(
        agent,
        initial_state=initial_state,
        finish=lambda run, final: {"ticks": len(final["log"])},
        recursion_limit=50,
    )
    other = GraphRunRef(KIND, uuid.uuid4())
    assert roomy.advance(other, checkpoints_for(store, other), None).output == {"ticks": 10}


# ------------------------------------------------------- what the library refuses
def declared_reserved_name() -> Agent:
    agent = declared_nothing()

    @agent.node(start=True)
    def __end__(state: State) -> dict[str, Any]:
        return {}

    return agent


def test_agent_the_library_refuses_is_refused_as_an_invalid_agent(store):
    with pytest.raises(InvalidAgent, match="LangGraph refuses.*__end__.*reserved"):
        compile_agent(declared_reserved_name(), store)


def test_agent_the_library_refuses_is_refused_when_its_steps_are_made_not_at_its_first_run():
    with pytest.raises(InvalidAgent, match="LangGraph refuses.*__end__.*reserved"):
        steps_of(declared_reserved_name())


def test_steps_keep_the_graph_the_agent_had_when_they_were_made(store, runs, run):
    agent = newsletter(runs)
    steps = steps_of(agent)

    @agent.node
    def intruder(state: State) -> dict[str, Any]:
        runs.append("intruder")
        return {}

    agent.edge(agent.shape().nodes["draft"].function, intruder)
    steps.advance(run, checkpoints_for(store, run), None)

    assert runs == ["draft", "send"]


def test_node_is_known_to_the_library_by_the_name_it_was_declared_with(store, runs):
    graph = compile_agent(newsletter(runs), store)

    assert [graph.nodes[name].bound.name for name in ("draft", "send", "apologise")] == [
        "draft",
        "send",
        "apologise",
    ]


# --------------------------------------------------- a node cannot choose where the graph goes
def test_node_that_answers_with_a_command_cannot_send_the_graph_where_no_edge_goes(store, run):
    agent = Agent(KIND, state=State)
    reached: list[str] = []

    @agent.node(start=True)
    def draft(state: State) -> Any:
        return Command(goto="forbidden", update={"log": [SECRET]})

    @agent.node
    def forbidden(state: State) -> dict[str, Any]:
        reached.append("forbidden")
        return {}

    with pytest.raises(InvalidNodeResult, match="node 'draft'") as refused:
        steps_of(agent).advance(run, checkpoints_for(store, run), None)

    assert reached == []
    assert SECRET not in str(refused.value)
    assert isinstance(refused.value, StepError)


# ------------------------------------------------------------- tools never ask for actions
def agent_with_a_tool_that_asks(runs: list[str]) -> Agent:
    agent = Agent(KIND, state=State)

    @agent.action("send_email")
    class SendEmail(BaseModel):
        model_config = ConfigDict(extra="forbid")

        to: str

    @agent.tool
    def mail_everyone() -> str:
        """Mail everyone."""
        request_action(SendEmail(to="everyone"))
        return "sent"

    @agent.node(start=True)
    def call_tools(state: State) -> dict[str, Any]:
        runs.append("call_tools")
        return {"log": [agent.call_tool("mail_everyone", {})]}

    return agent


def test_tool_that_asks_for_an_action_is_refused_and_the_graph_does_not_pause(store, runs, run):
    steps = steps_of(agent_with_a_tool_that_asks(runs))

    with pytest.raises(
        ActionAskedByTool, match="tool 'mail_everyone' asked for an action"
    ) as refused:
        steps.advance(run, checkpoints_for(store, run), None)

    assert isinstance(refused.value, StepError)
    assert runs == ["call_tools"]


def test_node_may_ask_for_an_action_after_it_called_a_tool(store, run):
    agent = Agent(KIND, state=State)

    @agent.action("send_email")
    class SendEmail(BaseModel):
        model_config = ConfigDict(extra="forbid")

        to: str

    @agent.tool
    def look() -> str:
        """Look."""
        return "looked"

    @agent.node(start=True)
    def look_then_ask(state: State) -> dict[str, Any]:
        agent.call_tool("look", {})
        outcome = request_action(SendEmail(to="ada"))
        return {"log": ["asked"], "delivered": outcome.succeeded}

    step = steps_of(agent).advance(run, checkpoints_for(store, run), None)

    assert waiting_action(step).name == "send_email"


def test_node_that_asks_with_a_model_that_was_never_declared_is_refused_before_it_asks():
    class SendEmail(BaseModel):
        model_config = ConfigDict(extra="forbid")

        to: str

    with pytest.raises(NotAnAction) as refused:
        request_action(SendEmail(to=SECRET))

    assert SECRET not in str(refused.value)


# ------------------------------------------------------------------ the run's context
@dataclass
class Mailer:
    """What the wiring builds for each attempt: it is never stored with the state."""

    attempt: int
    api_key: str = "key-4242-never-stored"


def agent_with_a_context(seen: list[str]) -> Agent:
    agent = Agent(KIND, state=State, context=Mailer)

    @agent.action("send_email")
    class SendEmail(BaseModel):
        model_config = ConfigDict(extra="forbid")

        to: str

    @agent.tool
    def key_length(context: Mailer) -> str:
        """The length of the key."""
        return str(len(context.api_key))

    @agent.node(start=True)
    def draft(state: State, mailer: Mailer) -> dict[str, Any]:
        seen.append(f"draft in attempt {mailer.attempt}")
        return {"log": [f"drafted in attempt {mailer.attempt}"]}

    @agent.node
    def send(state: State, mailer: Mailer) -> dict[str, Any]:
        key = agent.call_tool("key_length", {}, mailer)
        outcome = request_action(SendEmail(to=state["recipient"]))
        seen.append(f"send in attempt {mailer.attempt}")
        return {
            "log": [f"sent in attempt {mailer.attempt} with a key of {key} characters"],
            "delivered": outcome.succeeded,
        }

    @agent.node
    def apologise(state: State) -> dict[str, Any]:
        return {"log": ["apologised"]}

    agent.edge(draft, send)
    agent.edge(apologise, END)

    @agent.route(after=send, to=[apologise, END])
    def after_send(state: State, mailer: Mailer) -> str:
        seen.append(f"route in attempt {mailer.attempt}")
        return END if state["delivered"] else "apologise"

    return agent


class Attempts:
    """The wiring's context for each attempt: a new one every time it is asked for."""

    def __init__(self) -> None:
        self.asked_by: list[tuple[GraphRunRef, RunCheckpoints]] = []

    def __call__(self, run: GraphRunRef, checkpoints: RunCheckpoints) -> Mailer:
        self.asked_by.append((run, checkpoints))
        return Mailer(attempt=len(self.asked_by))


def contextual_steps(seen: list[str], attempts: Attempts) -> AgentSteps:
    return AgentSteps(
        agent_with_a_context(seen),
        initial_state=initial_state,
        finish=projection,
        context=attempts,
        recursion_limit=RECURSION_LIMIT,
    )


def test_nodes_routes_and_tools_are_given_the_context_of_the_attempt_that_runs_them(store, run):
    seen: list[str] = []
    attempts = Attempts()
    steps = contextual_steps(seen, attempts)
    checkpoints = checkpoints_for(store, run)
    action = waiting_action(steps.advance(run, checkpoints, None))

    finished = steps.advance(run, checkpoints, success(action))

    assert seen == [
        "draft in attempt 1",
        "send in attempt 2",
        "route in attempt 2",
    ]
    assert finished.output is not None
    assert finished.output["log"] == [
        "drafted in attempt 1",
        "sent in attempt 2 with a key of 21 characters",
    ]


def test_context_is_made_once_for_each_attempt_from_the_run_and_its_checkpoints(store, run):
    attempts = Attempts()
    steps = contextual_steps([], attempts)
    checkpoints = checkpoints_for(store, run)

    action = waiting_action(steps.advance(run, checkpoints, None))
    assert len(attempts.asked_by) == 1
    steps.advance(run, checkpoints, None)
    assert len(attempts.asked_by) == 2
    steps.advance(run, checkpoints, success(action))

    assert len(attempts.asked_by) == 3
    assert all(asked == (run, checkpoints) for asked in attempts.asked_by)


def test_context_is_not_stored_with_the_state(store, run):
    attempts = Attempts()
    steps = contextual_steps([], attempts)
    checkpoints = checkpoints_for(store, run)
    waiting_action(steps.advance(run, checkpoints, None))

    config: RunnableConfig = {"configurable": {"thread_id": checkpoints.thread_id}}
    stored = [repr(found.checkpoint) for found in store.list(config)]

    assert stored
    assert not any("key-4242-never-stored" in text for text in stored)


def test_agent_with_a_context_cannot_have_steps_without_one(store):
    with pytest.raises(InvalidAgent, match="declared the context type Mailer, and none was given"):
        AgentSteps(
            agent_with_a_context([]),
            initial_state=initial_state,
            finish=projection,
            recursion_limit=RECURSION_LIMIT,
        )


def test_agent_without_a_context_cannot_have_steps_with_one(runs):
    with pytest.raises(InvalidAgent, match="declared no context type, and a context was given"):
        AgentSteps(
            newsletter(runs),
            initial_state=initial_state,
            finish=projection,
            context=Attempts(),
            recursion_limit=RECURSION_LIMIT,
        )


# --------------------------------------------------- the first state is made only when needed
def test_initial_state_is_made_only_for_a_run_that_has_no_checkpoint_yet(store, runs, run):
    made: list[GraphRunRef] = []

    def initial_state_that_reads_a_database(for_run: GraphRunRef) -> dict[str, Any]:
        if made:
            raise AssertionError("a step that continues needs no initial state")
        made.append(for_run)
        return initial_state(for_run)

    steps = AgentSteps(
        newsletter(runs),
        initial_state=initial_state_that_reads_a_database,
        finish=projection,
        recursion_limit=RECURSION_LIMIT,
    )
    checkpoints = checkpoints_for(store, run)

    action = waiting_action(steps.advance(run, checkpoints, None))
    steps.advance(run, checkpoints, None)
    steps.advance(run, checkpoints, success(action))

    assert made == [run]


# ---------------------------------------------------------- running from a node, in memory
class Work(TypedDict):
    log: Annotated[list[str], operator.add]
    note: str


def agent_that_works_in_three_nodes(seen: list[str]) -> Agent:
    agent = Agent("worker", state=Work, context=Mailer)

    @agent.node(start=True)
    def prepare(state: Work) -> dict[str, Any]:
        seen.append("prepare")
        return {"log": ["prepared"]}

    @agent.node
    def work(state: Work, mailer: Mailer) -> dict[str, Any]:
        seen.append("work")
        return {"log": [f"worked in attempt {mailer.attempt}"]}

    @agent.node
    def report(state: Work) -> dict[str, Any]:
        seen.append("report")
        return {"log": ["reported"]}

    agent.edge(prepare, work)
    agent.edge(work, report)
    agent.edge(report, END)
    return agent


def test_graph_is_run_from_the_node_it_is_given_and_not_from_its_start():
    seen: list[str] = []

    final = run_from(
        agent_that_works_in_three_nodes(seen),
        "work",
        {"log": ["cloned"], "note": "n"},
        context=Mailer(attempt=7),
        recursion_limit=RECURSION_LIMIT,
    )

    assert seen == ["work", "report"]
    assert final == {"log": ["cloned", "worked in attempt 7", "reported"], "note": "n"}


def test_graph_is_run_from_its_start_node_too():
    seen: list[str] = []

    run_from(
        agent_that_works_in_three_nodes(seen),
        "prepare",
        {"log": [], "note": ""},
        context=Mailer(attempt=1),
        recursion_limit=RECURSION_LIMIT,
    )

    assert seen == ["prepare", "work", "report"]


def test_graph_run_from_a_node_keeps_the_routes_after_it():
    agent = Agent("worker", state=Work)

    @agent.node(start=True)
    def prepare(state: Work) -> dict[str, Any]:
        return {"log": ["prepared"]}

    @agent.node
    def tick(state: Work) -> dict[str, Any]:
        return {"log": ["tick"]}

    @agent.route(after=tick, to=[tick, END])
    def keep_going(state: Work) -> str:
        return END if len(state["log"]) >= 3 else "tick"

    final = run_from(agent, "tick", {"log": [], "note": ""}, recursion_limit=RECURSION_LIMIT)

    assert final["log"] == ["tick", "tick", "tick"]


def test_run_from_a_node_is_bounded_by_the_limit_it_is_given():
    agent = Agent("worker", state=Work)

    @agent.node(start=True)
    def tick(state: Work) -> dict[str, Any]:
        return {"log": ["tick"]}

    agent.route(after=tick, to=[tick])(lambda state: "tick")

    with pytest.raises(GraphRecursionError):
        run_from(agent, "tick", {"log": [], "note": ""}, recursion_limit=5)


def test_run_that_never_pauses_cannot_have_an_action_run_and_says_which_one_was_asked():
    agent = Agent(KIND, state=State)

    @agent.action("send_email")
    class SendEmail(BaseModel):
        model_config = ConfigDict(extra="forbid")

        to: str

    @agent.node(start=True)
    def send(state: State) -> dict[str, Any]:
        request_action(SendEmail(to=SECRET))
        return {}

    with pytest.raises(ActionAskedInLocalRun, match="action 'send_email'") as refused:
        run_from(agent, "send", {"log": []}, recursion_limit=RECURSION_LIMIT)

    assert refused.value.action_name == "send_email"
    assert SECRET not in str(refused.value)


def test_run_from_a_node_the_agent_does_not_have_is_refused():
    with pytest.raises(InvalidAgent, match="no node 'upload' to run from"):
        run_from(
            agent_that_works_in_three_nodes([]),
            "upload",
            {"log": [], "note": ""},
            context=Mailer(attempt=1),
            recursion_limit=RECURSION_LIMIT,
        )


def test_run_from_a_node_needs_the_context_of_an_agent_that_declared_one():
    with pytest.raises(InvalidAgent, match="declared the context type Mailer, and none was given"):
        run_from(
            agent_that_works_in_three_nodes([]),
            "work",
            {"log": [], "note": ""},
            recursion_limit=RECURSION_LIMIT,
        )


def test_run_from_a_node_refuses_a_context_for_an_agent_that_declared_none(runs):
    with pytest.raises(InvalidAgent, match="declared no context type, and a context was given"):
        run_from(
            newsletter(runs),
            "send",
            {"log": []},
            context=Mailer(attempt=1),
            recursion_limit=RECURSION_LIMIT,
        )


def test_agent_that_is_not_a_graph_cannot_be_run_from_a_node():
    with pytest.raises(InvalidAgent, match="no nodes"):
        run_from(declared_nothing(), "draft", {}, recursion_limit=RECURSION_LIMIT)


# ------------------------------------------------------- the PostgreSQL store
@pytest.fixture
def database_url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def test_pause_and_resume_survive_the_store_and_a_new_connection(database_url, run):
    runs: list[str] = []
    steps = steps_of(newsletter(runs))
    thread = thread_id(run.kind, run.run_id)

    with run_checkpoints(database_url, thread) as checkpoints:
        action = waiting_action(steps.advance(run, checkpoints, None))
    assert action.action_id.startswith(f"{thread}:")

    # The next attempt: a connection of its own, a graph compiled again, the same request.
    with run_checkpoints(database_url, thread) as checkpoints:
        runs_before = list(runs)
        assert waiting_action(steps.advance(run, checkpoints, None)) == action
        assert runs == runs_before
        # A result of several kinds of plain data comes back as it went in.
        reply = {"id": "m-1", "sent": True, "cost": 0.25, "tags": ["a", "b"], "none": None}
        finished = steps.advance(run, checkpoints, success(action, **reply))
    assert finished == StepOutcome.finished({"log": ["drafted", "sent"], "delivered": True})

    # An attempt after that one applied the outcome: nothing is applied or run again.
    with run_checkpoints(database_url, thread) as checkpoints:
        runs_before = list(runs)
        assert steps.advance(run, checkpoints, success(action, **reply)) == finished
        assert runs == runs_before
        checkpoints.forget()


def test_node_that_died_after_it_was_answered_is_given_the_same_answer_on_the_store(
    database_url, run
):
    runs: list[str] = []
    steps = steps_of(newsletter(runs, dies_once=[True]))
    thread = thread_id(run.kind, run.run_id)

    with run_checkpoints(database_url, thread) as checkpoints:
        action = waiting_action(steps.advance(run, checkpoints, None))
        outcome = success(action, message_id="m-1")
        with pytest.raises(RuntimeError, match="worker died"):
            steps.advance(run, checkpoints, outcome)

    with run_checkpoints(database_url, thread) as checkpoints:
        step = steps.advance(run, checkpoints, outcome)
        checkpoints.forget()

    assert step == StepOutcome.finished({"log": ["drafted", "sent"], "delivered": True})


# ------------------------------------------------------------ a step's progress
def node_names(node: str, state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    return [RunEvent("step", {"node": node, "logged": list(update.get("log", []))})]


def progress_steps(agent: Agent, recorded: list[tuple[GraphRunRef, tuple[RunEvent, ...]]]):
    def record(run: GraphRunRef, events: Sequence[RunEvent]) -> None:
        recorded.append((run, tuple(events)))

    return AgentSteps(
        agent,
        initial_state=initial_state,
        finish=projection,
        recursion_limit=RECURSION_LIMIT,
        record_progress=record,
    )


def test_progress_of_an_agent_that_declared_none_is_no_events(runs):
    assert newsletter(runs).progress_of("draft", {}, {"log": ["drafted"]}) == ()


def test_an_agent_declares_its_progress_once(runs):
    agent = newsletter(runs)
    agent.progress(node_names)

    with pytest.raises(InvalidAgent, match="twice"):
        agent.progress(node_names)


def test_each_node_of_a_step_is_recorded_with_its_changes_as_it_ends(store, runs, run):
    agent = newsletter(runs)
    agent.progress(node_names)
    recorded: list[tuple[GraphRunRef, tuple[RunEvent, ...]]] = []
    steps = progress_steps(agent, recorded)
    checkpoints = checkpoints_for(store, run)

    action = waiting_action(steps.advance(run, checkpoints, None))
    steps.advance(run, checkpoints, success(action))

    # ``send`` paused the first step: it shows nothing until it ends, with the outcome.
    assert recorded == [
        (run, (RunEvent("step", {"node": "draft", "logged": ["drafted"]}),)),
        (run, (RunEvent("step", {"node": "send", "logged": ["sent"]}),)),
    ]


def test_a_recorder_that_fails_does_not_fail_the_step(store, runs, run):
    agent = newsletter(runs)
    agent.progress(node_names)

    def broken(run: GraphRunRef, events: Sequence[RunEvent]) -> None:
        raise RuntimeError("the database is down")

    steps = AgentSteps(
        agent,
        initial_state=initial_state,
        finish=projection,
        recursion_limit=RECURSION_LIMIT,
        record_progress=broken,
    )

    step = steps.advance(run, checkpoints_for(store, run), None)

    assert waiting_action(step).name == "send_email"
