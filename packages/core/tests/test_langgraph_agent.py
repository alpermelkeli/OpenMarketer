"""Tests for ``compile_agent`` and ``AgentSteps``: an agent written with decorators, run for real.

The agents here are made of nothing but decorators and scripted nodes, with no model. Most tests
keep their checkpoints in memory; the last ones use the PostgreSQL store (fixtures in the
``conftest.py`` at the repository root), which stores them with the strict serializer.
"""

import inspect
import operator
import uuid
from collections.abc import Callable, Mapping
from typing import Annotated, Any, TypedDict

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END as LANGGRAPH_END
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine

from openmarketer_core.agent_runtime.actions import ActionOutcome, ActionRequest
from openmarketer_core.agent_runtime.agent import END, Agent, InvalidAgent, InvalidRouteChoice
from openmarketer_core.agent_runtime.langgraph_agent import AgentSteps, compile_agent
from openmarketer_core.agent_runtime.langgraph_steps import request_action
from openmarketer_core.agent_runtime.steps import GraphRunRef, RunOfAnotherKind, StepOutcome
from openmarketer_core.agent_runtime.threads import thread_id
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.graph_checkpoints.postgres import run_checkpoints

KIND = "newsletter"
# Far more graph steps than any test agent takes in one advance.
RECURSION_LIMIT = 50


class State(TypedDict):
    recipient: str
    log: Annotated[list[str], operator.add]
    delivered: bool
    reply: dict[str, Any]


def initial_state(run: GraphRunRef) -> dict[str, Any]:
    return {"recipient": f"{run.run_id}@example.com", "log": []}


def projection(final: Mapping[str, Any]) -> dict[str, Any]:
    """What a run reports: the log and whether it was delivered, not the state."""
    return {"log": final["log"], "delivered": final["delivered"]}


def newsletter(runs: list[str], dies_once: list[bool] | None = None) -> Agent:
    """An agent of decorators only: draft, send an e-mail (an action), and apologise on failure."""
    agent = Agent(KIND, state=State)

    @agent.action("send_email", timeout=10, retries=2, retry_after=1)
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


def steps_of(agent: Agent, result: Callable[[Mapping[str, Any]], dict[str, Any]] = projection):
    return AgentSteps(
        agent, initial_state=initial_state, result=result, recursion_limit=RECURSION_LIMIT
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


def test_agent_finishes_with_the_projection_of_its_state_and_not_the_state(store, runs, run):
    steps = steps_of(newsletter(runs))
    checkpoints = checkpoints_for(store, run)
    action = waiting_action(steps.advance(run, checkpoints, None))

    step = steps.advance(run, checkpoints, success(action, message_id="m-1"))

    assert step == StepOutcome.finished({"log": ["drafted", "sent"], "delivered": True})


def test_result_is_given_the_whole_final_state(store, runs, run):
    seen: list[Mapping[str, Any]] = []

    def result(final: Mapping[str, Any]) -> dict[str, Any]:
        seen.append(final)
        return {}

    steps = steps_of(newsletter(runs), result)
    checkpoints = checkpoints_for(store, run)
    action = waiting_action(steps.advance(run, checkpoints, None))

    steps.advance(run, checkpoints, success(action, message_id="m-1"))

    (final,) = seen
    assert final["reply"] == {"message_id": "m-1"}
    assert final["recipient"] == f"{run.run_id}@example.com"


def test_result_is_not_asked_for_while_the_agent_waits(store, runs, run):
    def result(final: Mapping[str, Any]) -> dict[str, Any]:
        raise AssertionError("the run has not finished")

    steps_of(newsletter(runs), result).advance(run, checkpoints_for(store, run), None)


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

    limited = AgentSteps(agent, initial_state=initial_state, result=projection, recursion_limit=5)
    with pytest.raises(GraphRecursionError):
        limited.advance(run, checkpoints_for(store, run), None)

    roomy = AgentSteps(
        agent,
        initial_state=initial_state,
        result=lambda final: {"ticks": len(final["log"])},
        recursion_limit=50,
    )
    other = GraphRunRef(KIND, uuid.uuid4())
    assert roomy.advance(other, checkpoints_for(store, other), None).output == {"ticks": 10}


@pytest.mark.parametrize("left_out", ["result", "recursion_limit"])
def test_what_a_run_reports_and_how_long_it_may_take_have_no_default(runs, left_out):
    given: dict[str, Any] = {
        "initial_state": initial_state,
        "result": projection,
        "recursion_limit": RECURSION_LIMIT,
    }
    del given[left_out]

    with pytest.raises(TypeError, match=left_out):
        AgentSteps(newsletter(runs), **given)


def test_steps_have_the_shape_of_the_workers_agent_graph():
    # The worker's ``AgentGraph`` protocol, which core does not import.
    parameters = inspect.signature(AgentSteps.advance).parameters
    assert list(parameters) == ["self", "run", "checkpoints", "resume"]


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
