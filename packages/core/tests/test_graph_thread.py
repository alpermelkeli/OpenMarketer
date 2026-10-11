"""Tests for ``GraphThread`` and ``request_action``: a real LangGraph graph, no model.

The graphs here are scripted: nodes append to a list of runs, so a test can say
how often a node ran. Most tests keep their checkpoints in memory; the last
ones use the PostgreSQL store (fixtures in the ``conftest.py`` at the
repository root), which stores them with the strict serializer.
"""

import contextvars
import operator
import uuid
from dataclasses import asdict
from typing import Annotated, Any, Protocol, TypedDict

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, text

from openmarketer_core.agent_runtime.actions import ActionOutcome, ActionRequest
from openmarketer_core.agent_runtime.agent import Agent
from openmarketer_core.agent_runtime.langgraph_steps import (
    CompiledGraph,
    GraphThread,
    request_action,
)
from openmarketer_core.agent_runtime.steps import (
    MalformedActionRequest,
    MissingInitialState,
    RequestChangedOnResume,
    TooManyPendingActions,
    TwoActionsInOneNode,
    UnexpectedResume,
)
from openmarketer_core.agent_runtime.threads import thread_id
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.graph_checkpoints.postgres import run_checkpoints

START_STATE = {"log": [], "answers": []}
# Far more graph steps than any test graph takes in one advance.
RECURSION_LIMIT = 50


class State(TypedDict):
    log: Annotated[list[str], operator.add]
    answers: Annotated[list[dict[str, Any]], operator.add]


agent = Agent("graph-thread-test", state=State)


@agent.action("send_email")
class SendEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: str


class Node(Protocol):
    def __call__(self, state: State) -> dict[str, Any]: ...


def asks_to_send(name: str, runs: list[str]) -> Node:
    """A node that asks for an e-mail to ``name`` and records what it was told."""

    def node(state: State) -> dict[str, Any]:
        runs.append(name)
        outcome = request_action(SendEmail(to=name))
        return {"log": [f"{name} done"], "answers": [asdict(outcome)]}

    return node


def asks_twice(name: str, runs: list[str]) -> Node:
    """A node that asks for two e-mails one after the other, which a node may not."""

    def node(state: State) -> dict[str, Any]:
        runs.append(name)
        first = request_action(SendEmail(to="first"))
        second = request_action(SendEmail(to="second"))
        return {"answers": [asdict(first), asdict(second)]}

    return node


def asks_twice_from_a_context_that_does_not_know_of_the_first(name: str, runs: list[str]) -> Node:
    """Like ``asks_twice``, the second time from a context copied before the first answer."""

    def node(state: State) -> dict[str, Any]:
        runs.append(name)
        before_the_first = contextvars.copy_context()
        first = request_action(SendEmail(to="first"))
        second = before_the_first.run(request_action, SendEmail(to="second"))
        return {"answers": [asdict(first), asdict(second)]}

    return node


def asks_for_a_new_draft_each_time(name: str, runs: list[str]) -> Node:
    """A node that does not ask for the same thing each time it runs, as one that drafts would."""

    def node(state: State) -> dict[str, Any]:
        runs.append(name)
        outcome = request_action(SendEmail(to=f"draft-{len(runs)}"))
        return {"answers": [asdict(outcome)]}

    return node


def records(name: str, runs: list[str]) -> Node:
    def node(state: State) -> dict[str, Any]:
        runs.append(name)
        return {"log": [f"{name} done"]}

    return node


def chain(store: BaseCheckpointSaver[str], **nodes: Node) -> CompiledGraph:
    """START, the nodes in the order given, END."""
    builder = StateGraph(State)
    previous = START
    for name, node in nodes.items():
        builder.add_node(name, node)
        builder.add_edge(previous, name)
        previous = name
    builder.add_edge(previous, END)
    return builder.compile(checkpointer=store)


def checkpoints_in(store: BaseCheckpointSaver[str], run: str = "test-run") -> RunCheckpoints:
    return RunCheckpoints(store=store, thread_id=f"test:{run}")


def success(request: ActionRequest, **result: Any) -> ActionOutcome:
    return ActionOutcome(action_id=request.action_id, succeeded=True, result=result, error="")


def failure(request: ActionRequest, error: str) -> ActionOutcome:
    return ActionOutcome(action_id=request.action_id, succeeded=False, result={}, error=error)


@pytest.fixture
def store() -> InMemorySaver:
    return InMemorySaver()


@pytest.fixture
def runs() -> list[str]:
    return []


@pytest.fixture
def paused(store, runs) -> GraphThread:
    """A thread whose graph asked for one e-mail and waits for it."""
    thread = GraphThread(
        chain(store, prepare=records("prepare", runs), send=asks_to_send("send", runs)),
        checkpoints_in(store),
        recursion_limit=RECURSION_LIMIT,
    )
    assert thread.advance(initial=START_STATE).action is not None
    return thread


# ----------------------------------------------------------- pausing at an action
def test_graph_pauses_at_the_action_a_node_asks_for(store, runs):
    thread = GraphThread(
        chain(store, send=asks_to_send("ada", runs)),
        checkpoints_in(store),
        recursion_limit=RECURSION_LIMIT,
    )

    step = thread.advance(initial=START_STATE)

    assert step.output is None
    assert step.action is not None
    assert (step.action.name, step.action.payload) == ("send_email", {"to": "ada"})


def test_work_before_the_action_is_kept_while_the_graph_waits(paused):
    assert paused.state().values["log"] == ["prepare done"]


def test_id_of_the_action_starts_with_the_thread_and_is_the_same_each_time_it_is_read(paused):
    first = paused.advance()
    assert first.action is not None
    assert first.action.action_id.startswith("test:test-run:")
    assert paused.state().pending_action == first.action
    assert paused.advance().action == first.action


def test_id_of_the_action_is_the_same_from_a_graph_built_again_on_the_same_store(
    store, runs, paused
):
    asked = paused.state().pending_action
    rebuilt = GraphThread(
        chain(store, prepare=records("prepare", runs), send=asks_to_send("send", runs)),
        checkpoints_in(store),
        recursion_limit=RECURSION_LIMIT,
    )
    assert rebuilt.state().pending_action == asked
    assert rebuilt.advance().action == asked


def test_id_of_the_action_is_the_same_when_the_step_that_asked_runs_again(store, runs, paused):
    # What the idempotency key relies on: the library derives the id from the checkpoint, so a
    # step that is attempted again, as after a worker that died, asks with the same id.
    asked = paused.state().pending_action
    graph = chain(store, prepare=records("prepare", runs), send=asks_to_send("send", runs))
    runs_before = len(runs)

    graph.invoke(None, {"configurable": {"thread_id": "test:test-run"}})

    assert len(runs) == runs_before + 1
    assert (
        GraphThread(graph, checkpoints_in(store), recursion_limit=RECURSION_LIMIT)
        .state()
        .pending_action
        == asked
    )


def test_attempt_repeated_on_a_paused_thread_runs_nothing(paused, runs):
    asked = paused.state().pending_action
    runs_before = list(runs)

    again = paused.advance()

    assert again.action == asked
    assert runs == runs_before


# ------------------------------------------------------------- resuming with an outcome
def test_outcome_is_applied_and_the_graph_finishes(paused):
    request = paused.state().pending_action
    assert request is not None

    step = paused.advance(resume=success(request, message_id="m-1"))

    assert step.action is None
    assert step.output is not None
    assert step.output["log"] == ["prepare done", "send done"]
    assert step.output["answers"] == [
        {
            "action_id": request.action_id,
            "succeeded": True,
            "result": {"message_id": "m-1"},
            "error": "",
        }
    ]


def test_node_before_the_action_does_not_run_again_after_the_outcome(paused, runs):
    request = paused.state().pending_action
    assert request is not None

    paused.advance(resume=success(request))

    assert runs == ["prepare", "send", "send"]


def test_outcome_that_an_earlier_attempt_applied_is_not_applied_twice(paused, runs):
    request = paused.state().pending_action
    assert request is not None
    outcome = success(request, message_id="m-1")
    first = paused.advance(resume=outcome)
    runs_before = list(runs)

    again = paused.advance(resume=outcome)

    assert again == first
    assert runs == runs_before
    assert again.output is not None
    assert len(again.output["answers"]) == 1


def test_failed_outcome_reaches_the_node_as_data(paused):
    request = paused.state().pending_action
    assert request is not None

    step = paused.advance(resume=failure(request, "the mail server refused the message"))

    assert step.output is not None
    (answer,) = step.output["answers"]
    assert (answer["succeeded"], answer["error"]) == (False, "the mail server refused the message")


def test_node_that_failed_after_it_was_answered_gets_the_same_answer_when_attempted_again(store):
    broken = [True]

    def asks_then_breaks(state: State) -> dict[str, Any]:
        outcome = request_action(SendEmail(to="ada"))
        if broken:
            broken.pop()
            raise RuntimeError("the worker died")
        return {"log": ["sent"], "answers": [asdict(outcome)]}

    thread = GraphThread(
        chain(store, send=asks_then_breaks), checkpoints_in(store), recursion_limit=RECURSION_LIMIT
    )
    request = thread.advance(initial=START_STATE).action
    assert request is not None
    outcome = success(request, message_id="m-1")
    with pytest.raises(RuntimeError, match="worker died"):
        thread.advance(resume=outcome)

    step = thread.advance(resume=outcome)

    assert step.output is not None
    assert step.output["answers"] == [asdict(outcome)]


# ------------------------------------------------------------ several actions in a row
def test_outcome_of_an_action_that_is_no_longer_pending_returns_the_next_request(store, runs):
    thread = GraphThread(
        chain(store, first=asks_to_send("first", runs), second=asks_to_send("second", runs)),
        checkpoints_in(store),
        recursion_limit=RECURSION_LIMIT,
    )
    first = thread.advance(initial=START_STATE).action
    assert first is not None
    outcome = success(first)
    second = thread.advance(resume=outcome).action
    assert second is not None and second != first
    runs_before = list(runs)

    # The attempt that applied the first outcome died before it could say what came next.
    again = thread.advance(resume=outcome)

    assert again.action == second
    assert runs == runs_before
    assert thread.advance(resume=success(second)).output is not None


# ------------------------------------------- one action per node, and what an outcome answers
def test_node_that_asks_for_a_second_action_is_refused_when_the_first_is_answered(store, runs):
    thread = GraphThread(
        chain(store, twice=asks_twice("twice", runs)),
        checkpoints_in(store),
        recursion_limit=RECURSION_LIMIT,
    )
    first = thread.advance(initial=START_STATE).action
    assert first is not None

    with pytest.raises(TwoActionsInOneNode, match=first.action_id):
        thread.advance(resume=success(first, sent="first"))


def test_second_action_of_a_node_is_not_given_the_outcome_of_the_first_when_attempted_again(
    store, runs
):
    thread = GraphThread(
        chain(store, twice=asks_twice("twice", runs)),
        checkpoints_in(store),
        recursion_limit=RECURSION_LIMIT,
    )
    first = thread.advance(initial=START_STATE).action
    assert first is not None
    outcome = success(first, sent="first")
    with pytest.raises(TwoActionsInOneNode):
        thread.advance(resume=outcome)

    # The attempt that applied the outcome died; the next one is given the same outcome.
    with pytest.raises(TwoActionsInOneNode):
        thread.advance(resume=outcome)

    state = thread.state()
    assert state.values["answers"] == []
    assert state.pending_action == first, "the second request was asked as well"


def test_second_action_asked_where_the_node_cannot_be_watched_is_refused_when_the_step_waits(
    store, runs
):
    node = asks_twice_from_a_context_that_does_not_know_of_the_first("twice", runs)
    thread = GraphThread(
        chain(store, twice=node), checkpoints_in(store), recursion_limit=RECURSION_LIMIT
    )
    first = thread.advance(initial=START_STATE).action
    assert first is not None

    with pytest.raises(TwoActionsInOneNode, match=first.action_id):
        thread.advance(resume=success(first))


def test_outcome_is_not_delivered_to_a_node_that_asks_for_something_else_when_it_runs_again(
    store, runs
):
    thread = GraphThread(
        chain(store, draft=asks_for_a_new_draft_each_time("draft", runs)),
        checkpoints_in(store),
        recursion_limit=RECURSION_LIMIT,
    )
    asked = thread.advance(initial=START_STATE).action
    assert asked is not None and asked.payload == {"to": "draft-1"}

    with pytest.raises(RequestChangedOnResume, match=asked.action_id) as refused:
        thread.advance(resume=success(asked, sent="draft-1"))

    assert "draft" not in str(refused.value)
    state = thread.state()
    assert state.values["answers"] == []
    assert state.pending_action == asked


# --------------------------------------------------------------------- refusals
def test_outcome_for_an_action_of_another_thread_is_refused(paused, runs):
    request = paused.state().pending_action
    assert request is not None
    foreign = ActionOutcome(
        action_id=request.action_id.replace("test:test-run", "test:other-run"),
        succeeded=True,
        result={},
        error="",
    )
    runs_before = list(runs)

    with pytest.raises(UnexpectedResume, match="other-run"):
        paused.advance(resume=foreign)

    assert runs == runs_before
    assert paused.state().pending_action == request


def test_outcome_for_an_action_that_was_never_asked_is_refused(paused):
    request = paused.state().pending_action
    assert request is not None
    invented = ActionOutcome(
        action_id="test:test-run:0123456789abcdef0123456789abcdef",
        succeeded=True,
        result={},
        error="",
    )

    with pytest.raises(UnexpectedResume):
        paused.advance(resume=invented)

    assert paused.state().pending_action == request


def test_outcome_on_a_thread_that_asked_for_nothing_is_refused(store, runs):
    thread = GraphThread(
        chain(store, send=asks_to_send("ada", runs)),
        checkpoints_in(store),
        recursion_limit=RECURSION_LIMIT,
    )
    outcome = ActionOutcome(action_id="test:test-run:abc", succeeded=True, result={}, error="")

    with pytest.raises(UnexpectedResume):
        thread.advance(initial=START_STATE, resume=outcome)

    assert runs == []


def test_graph_that_waits_for_two_actions_at_once_is_refused(store, runs):
    builder = StateGraph(State)
    builder.add_node("ada", asks_to_send("ada", runs))
    builder.add_node("grace", asks_to_send("grace", runs))
    builder.add_edge(START, "ada")
    builder.add_edge(START, "grace")
    builder.add_edge("ada", END)
    builder.add_edge("grace", END)
    thread = GraphThread(
        builder.compile(checkpointer=store), checkpoints_in(store), recursion_limit=RECURSION_LIMIT
    )

    with pytest.raises(TooManyPendingActions, match="2 actions"):
        thread.advance(initial=START_STATE)
    with pytest.raises(TooManyPendingActions):
        thread.state()


def test_node_that_pauses_for_anything_but_an_action_request_is_refused(store):
    def approves(state: State) -> dict[str, Any]:
        interrupt("shall I go on?")
        return {}

    thread = GraphThread(
        chain(store, approve=approves), checkpoints_in(store), recursion_limit=RECURSION_LIMIT
    )

    with pytest.raises(MalformedActionRequest):
        thread.advance(initial=START_STATE)


def test_empty_thread_cannot_start_without_an_initial_state(store, runs):
    thread = GraphThread(
        chain(store, step=records("step", runs)),
        checkpoints_in(store),
        recursion_limit=RECURSION_LIMIT,
    )

    with pytest.raises(MissingInitialState, match="test:test-run"):
        thread.advance()

    assert runs == []


def test_steps_of_an_advance_must_be_bounded_by_the_caller(store, runs):
    graph = chain(store, step=records("step", runs))
    with pytest.raises(TypeError, match="recursion_limit"):
        GraphThread(graph, checkpoints_in(store))  # pyright: ignore[reportCallIssue]


def test_graph_compiled_with_another_store_is_refused(store, runs):
    graph = chain(InMemorySaver(), step=records("step", runs))
    with pytest.raises(ValueError, match="checkpoint store"):
        GraphThread(graph, checkpoints_in(store), recursion_limit=RECURSION_LIMIT)


# ------------------------------------------------- a thread without a pause, and its end
def test_thread_that_was_never_started_has_no_checkpoint(store, runs):
    thread = GraphThread(
        chain(store, step=records("step", runs)),
        checkpoints_in(store),
        recursion_limit=RECURSION_LIMIT,
    )

    state = thread.state()

    assert (state.has_checkpoint, state.pending_action, state.values) == (False, None, {})


def test_graph_that_asks_for_nothing_finishes_in_one_step(store, runs):
    thread = GraphThread(
        chain(store, step=records("step", runs)),
        checkpoints_in(store),
        recursion_limit=RECURSION_LIMIT,
    )

    step = thread.advance(initial=START_STATE)

    assert step.action is None
    assert step.output == {"log": ["step done"], "answers": []}


def test_step_on_a_finished_thread_returns_its_output_and_runs_nothing(store, runs):
    thread = GraphThread(
        chain(store, step=records("step", runs)),
        checkpoints_in(store),
        recursion_limit=RECURSION_LIMIT,
    )
    finished = thread.advance(initial=START_STATE)

    assert thread.advance() == finished
    assert thread.advance(initial={"log": ["another"], "answers": []}) == finished
    assert runs == ["step"]


def test_initial_state_is_ignored_when_the_thread_has_a_checkpoint(paused):
    asked = paused.state().pending_action

    assert paused.advance(initial={"log": ["another"], "answers": []}).action == asked
    assert paused.state().values["log"] == ["prepare done"]


def test_recursion_limit_bounds_the_graph_steps_of_one_advance(store):
    def count_up(state: State) -> dict[str, Any]:
        return {"log": ["tick"]}

    def keep_going(state: State) -> str:
        return END if len(state["log"]) >= 10 else "tick"

    builder = StateGraph(State)
    builder.add_node("tick", count_up)
    builder.add_edge(START, "tick")
    builder.add_conditional_edges("tick", keep_going, ["tick", END])
    graph = builder.compile(checkpointer=store)

    limited = GraphThread(graph, checkpoints_in(store, "limited"), recursion_limit=5)
    with pytest.raises(GraphRecursionError):
        limited.advance(initial=START_STATE)

    roomy = GraphThread(graph, checkpoints_in(store, "roomy"), recursion_limit=50)
    output = roomy.advance(initial=START_STATE).output
    assert output is not None and len(output["log"]) == 10


def test_forgetting_removes_the_checkpoints_of_the_thread(paused):
    paused.forget()

    state = paused.state()

    assert (state.has_checkpoint, state.pending_action) == (False, None)


# ------------------------------------------------------- the PostgreSQL store
@pytest.fixture
def database_url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


@pytest.fixture
def run_thread() -> str:
    return thread_id("graph-thread-test", uuid.uuid4())


def stored_threads(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(conn.execute(text("SELECT thread_id FROM checkpoints")).scalars())


def test_pause_and_resume_survive_the_store_and_a_new_connection(database_url, run_thread):
    runs: list[str] = []

    def graph_on(run: RunCheckpoints) -> GraphThread:
        nodes = {"prepare": records("prepare", runs), "send": asks_to_send("send", runs)}
        return GraphThread(chain(run.store, **nodes), run, recursion_limit=RECURSION_LIMIT)

    with run_checkpoints(database_url, run_thread) as run:
        asked = graph_on(run).advance(initial=START_STATE).action
    assert asked is not None
    assert asked.action_id.startswith(f"{run_thread}:")

    # The next attempt: a connection of its own, a graph built again, the same request.
    with run_checkpoints(database_url, run_thread) as run:
        thread = graph_on(run)
        assert thread.state().pending_action == asked
        runs_before = list(runs)
        assert thread.advance().action == asked
        assert runs == runs_before
        # A result of several kinds of plain data comes back as it went in.
        result = {"id": "m-1", "sent": True, "cost": 0.25, "tags": ["a", "b"], "reply": None}
        finished = thread.advance(resume=success(asked, **result))
    assert finished.output is not None
    assert finished.output["answers"][0]["result"] == result

    # An attempt after that one applied the outcome: nothing is applied or run again.
    with run_checkpoints(database_url, run_thread) as run:
        thread = graph_on(run)
        runs_before = list(runs)
        assert thread.advance(resume=success(asked, **result)) == finished
        assert runs == runs_before
        thread.forget()
        assert not thread.state().has_checkpoint


def test_second_action_waits_across_attempts_and_the_first_outcome_is_not_applied_again(
    database_url, run_thread
):
    runs: list[str] = []

    def graph_on(run: RunCheckpoints) -> GraphThread:
        nodes = {"first": asks_to_send("first", runs), "second": asks_to_send("second", runs)}
        return GraphThread(chain(run.store, **nodes), run, recursion_limit=RECURSION_LIMIT)

    with run_checkpoints(database_url, run_thread) as run:
        first = graph_on(run).advance(initial=START_STATE).action
        assert first is not None
        second = graph_on(run).advance(resume=success(first)).action
    assert second is not None and second != first

    with run_checkpoints(database_url, run_thread) as run:
        thread = graph_on(run)
        runs_before = list(runs)
        assert thread.advance(resume=success(first)).action == second
        assert runs == runs_before
        finished = thread.advance(resume=success(second))
        thread.forget()
    assert finished.output is not None
    assert [answer["action_id"] for answer in finished.output["answers"]] == [
        first.action_id,
        second.action_id,
    ]


@agent.action("publish")
class Rich(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ratio: float
    count: int
    flag: bool
    note: str | None
    tags: list[str]
    nested: dict[str, list[int]]


def asks_with_a_payload_of_many_kinds(runs: list[str]) -> Node:
    def node(state: State) -> dict[str, Any]:
        runs.append("rich")
        payload = Rich(
            ratio=0.1 + 0.2,
            count=2**40,
            flag=False,
            note=None,
            tags=["ünï", "\u2603"],
            nested={"b": [3, 1], "a": []},
        )
        return {"answers": [asdict(request_action(payload))]}

    return node


def test_request_of_a_payload_of_many_kinds_of_values_is_still_the_same_after_the_store(
    database_url, run_thread
):
    runs: list[str] = []

    def thread_on(run: RunCheckpoints) -> GraphThread:
        graph = chain(run.store, rich=asks_with_a_payload_of_many_kinds(runs))
        return GraphThread(graph, run, recursion_limit=RECURSION_LIMIT)

    with run_checkpoints(database_url, run_thread) as run:
        asked = thread_on(run).advance(initial=START_STATE).action
    assert asked is not None

    with run_checkpoints(database_url, run_thread) as run:
        thread = thread_on(run)
        assert thread.state().pending_action == asked
        finished = thread.advance(resume=success(asked))
        thread.forget()

    assert finished.output is not None and len(finished.output["answers"]) == 1


def test_node_that_asks_twice_is_refused_again_by_the_next_attempt_on_the_store(
    database_url, run_thread
):
    runs: list[str] = []

    def thread_on(run: RunCheckpoints) -> GraphThread:
        graph = chain(run.store, twice=asks_twice("twice", runs))
        return GraphThread(graph, run, recursion_limit=RECURSION_LIMIT)

    with run_checkpoints(database_url, run_thread) as run:
        first = thread_on(run).advance(initial=START_STATE).action
        assert first is not None
        with pytest.raises(TwoActionsInOneNode):
            thread_on(run).advance(resume=success(first))

    with run_checkpoints(database_url, run_thread) as run:
        thread = thread_on(run)
        with pytest.raises(TwoActionsInOneNode):
            thread.advance(resume=success(first))
        assert thread.state().pending_action == first
        thread.forget()


def test_forgetting_through_the_graph_thread_removes_the_stored_checkpoints(
    database_url, run_thread, engine
):
    runs: list[str] = []
    with run_checkpoints(database_url, run_thread) as run:
        thread = GraphThread(
            chain(run.store, send=asks_to_send("send", runs)), run, recursion_limit=RECURSION_LIMIT
        )
        thread.advance(initial=START_STATE)
        assert run_thread in stored_threads(engine)
        thread.forget()
    assert run_thread not in stored_threads(engine)
