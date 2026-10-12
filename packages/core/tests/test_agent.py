"""Tests for ``Agent``: the actions and the graph an agent declares with decorators, and what is
refused. The tools are in ``test_tools.py``.

``Agent`` is framework-free, so nothing here builds a graph; ``test_langgraph_agent.py`` does.
A payload model carries the name of its action once, so a test that needs a model makes a new
one (``new_model``) instead of sharing a class between tests.
"""

import functools
import importlib.util
import uuid
from dataclasses import dataclass
from typing import Any, TypedDict

import pytest
from pydantic import BaseModel, ConfigDict

from openmarketer_core.agent_runtime.actions import (
    ActionPolicy,
    InvalidActionDefinition,
    NotAnAction,
    action_name_of,
)
from openmarketer_core.agent_runtime.agent import (
    END,
    Agent,
    InvalidAgent,
    InvalidRouteChoice,
)
from openmarketer_core.agent_runtime.steps import InvalidNodeResult
from openmarketer_core.agent_runtime.threads import thread_id

SECRET = "sk-live-4242-do-not-repeat"


class State(TypedDict):
    value: str


def new_model(**fields: Any) -> type[BaseModel]:
    """A payload model that forbids extra fields and has not been registered as an action."""
    namespace = {"model_config": ConfigDict(extra="forbid"), "__annotations__": {"to": str}}
    return type("SendEmail", (BaseModel,), namespace | fields)


@pytest.fixture
def agent() -> Agent:
    return Agent("test-agent", state=State)


# ------------------------------------------------------------------ actions
def test_action_is_declared_with_a_decorator_on_the_model(agent):
    @agent.action("send_email")
    class SendEmail(BaseModel):
        model_config = ConfigDict(extra="forbid")

        to: str

    assert action_name_of(SendEmail(to="ada")) == "send_email"
    assert [definition.name for definition in agent.definitions] == ["send_email"]
    assert agent.definitions[0].payload is SendEmail


def test_definitions_are_in_the_order_they_were_declared(agent):
    for name in ("publish", "send_email", "archive"):
        agent.action(name)(new_model())

    assert [definition.name for definition in agent.definitions] == [
        "publish",
        "send_email",
        "archive",
    ]


def test_action_that_says_nothing_of_its_limits_is_tried_once_and_repeats_are_a_choice(agent):
    agent.action("send_email")(new_model())

    assert agent.policies["send_email"] == ActionPolicy(
        attempt_timeout_seconds=30, max_attempts=1, retry_after_seconds=1
    )


def test_limits_of_an_action_become_its_policy(agent):
    agent.action("render", timeout=600, attempts=2, retry_after=5, heartbeat=60)(new_model())

    assert agent.policies["render"] == ActionPolicy(
        attempt_timeout_seconds=600,
        max_attempts=2,
        retry_after_seconds=5,
        heartbeat_timeout_seconds=60,
    )


@pytest.mark.parametrize(
    ("limit", "value"),
    [
        ("timeout", 0),
        ("attempts", 0),
        ("retry_after", 0),
        ("heartbeat", 0),
        ("heartbeat", -1),
        ("timeout", float("nan")),
        ("retry_after", float("inf")),
        ("attempts", True),
    ],
)
def test_limit_that_is_not_a_positive_number_is_refused_on_the_line_of_the_decorator(
    agent, limit, value
):
    with pytest.raises(ValueError, match="more than 0"):
        agent.action("send_email", **{limit: value})


@pytest.mark.parametrize(
    ("limit", "value"),
    [("attempts", 11), ("timeout", 24 * 60 * 60 + 1), ("retry_after", 60 * 60 + 1)],
)
def test_limit_beyond_what_a_policy_allows_is_refused_on_the_line_of_the_decorator(
    agent, limit, value
):
    with pytest.raises(ValueError, match="at most"):
        agent.action("send_email", **{limit: value})


def test_policies_are_a_read_only_view(agent):
    agent.action("send_email")(new_model())

    with pytest.raises(TypeError):
        agent.policies["publish"] = ActionPolicy(1, 1, 1)  # pyright: ignore[reportIndexIssue]
    assert list(agent.policies) == ["send_email"]


def test_action_name_is_not_declared_twice(agent):
    agent.action("send_email")(new_model())

    with pytest.raises(InvalidActionDefinition, match="send_email"):
        agent.action("send_email")(new_model())

    assert len(agent.definitions) == 1


def test_model_that_was_refused_carries_no_name(agent):
    agent.action("send_email")(new_model())
    refused = new_model()

    with pytest.raises(InvalidActionDefinition):
        agent.action("send_email")(refused)

    with pytest.raises(NotAnAction):
        action_name_of(refused(to="ada"))


def test_model_is_not_the_payload_of_two_actions(agent):
    model = new_model()
    agent.action("send_email")(model)

    with pytest.raises(InvalidActionDefinition, match="already the payload of action 'send_email'"):
        agent.action("publish")(model)

    assert action_name_of(model(to="ada")) == "send_email"
    assert list(agent.policies) == ["send_email"]


def test_model_is_not_the_payload_of_an_action_of_another_agent_either(agent):
    model = new_model()
    agent.action("send_email")(model)

    with pytest.raises(InvalidActionDefinition, match="already the payload"):
        Agent("another-agent", state=State).action("publish")(model)


def test_model_that_allows_extra_fields_cannot_be_an_action(agent):
    lenient = new_model(model_config=ConfigDict(extra="ignore"))

    with pytest.raises(InvalidActionDefinition, match="forbid extra fields"):
        agent.action("send_email")(lenient)

    assert agent.definitions == ()
    with pytest.raises(NotAnAction):
        action_name_of(lenient(to="ada"))


def test_model_that_holds_one_that_allows_extra_fields_cannot_be_an_action(agent):
    class Lenient(BaseModel):
        value: str

    class Holder(BaseModel):
        model_config = ConfigDict(extra="forbid")

        inner: list[Lenient]

    with pytest.raises(InvalidActionDefinition, match="Lenient"):
        agent.action("hold")(Holder)


def test_subclass_of_an_action_model_is_not_an_action_until_it_is_declared(agent):
    @agent.action("send_email")
    class SendEmail(BaseModel):
        model_config = ConfigDict(extra="forbid")

        to: str

    class Urgent(SendEmail):
        pass

    with pytest.raises(NotAnAction, match="Urgent"):
        action_name_of(Urgent(to="ada"))


def test_refusal_of_a_model_that_was_never_declared_names_the_class_and_never_the_payload():
    with pytest.raises(NotAnAction, match="SendEmail") as refused:
        action_name_of(new_model()(to=SECRET))

    assert SECRET not in str(refused.value)


# --------------------------------------------------------------------- kind
@pytest.mark.parametrize("kind", ["analysis-run", "post-writer-2", "a"])
def test_kind_that_can_name_a_thread_is_accepted(kind):
    assert Agent(kind, state=State).kind == kind


@pytest.mark.parametrize(
    "kind",
    ["", "Analysis", "analysis_run", "analysis run", "analysis:run", "100%", "run\n", "análisis"],
)
def test_kind_is_refused_by_the_rule_that_names_threads(kind):
    with pytest.raises(ValueError, match="kind of run") as refused_for_thread:
        thread_id(kind, uuid.uuid4())
    with pytest.raises(ValueError, match="kind of run") as refused_for_agent:
        Agent(kind, state=State)

    assert str(refused_for_agent.value) == str(refused_for_thread.value)


# ---------------------------------------------------------------- isolation
def build_agent() -> Agent:
    """What a module that defines an agent does each time it is loaded."""
    built = Agent("test-agent", state=State)
    built.action("send_email")(new_model())

    @built.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    @built.tool
    def look(pattern: str) -> str:
        """Look for a pattern."""
        return pattern

    return built


def test_two_agents_share_no_registry():
    one, other = build_agent(), build_agent()
    other.action("publish")(new_model())

    assert [d.name for d in one.definitions] == ["send_email"]
    assert [d.name for d in other.definitions] == ["send_email", "publish"]
    assert list(one.shape().nodes) == list(other.shape().nodes) == ["draft"]
    assert one.shape().nodes["draft"].function is not other.shape().nodes["draft"].function


def test_loading_the_module_that_defines_an_agent_twice_gives_two_agents_and_no_error(tmp_path):
    module = tmp_path / "defines_an_agent.py"
    module.write_text(
        "from typing import TypedDict\n"
        "from pydantic import BaseModel, ConfigDict\n"
        "from openmarketer_core.agent_runtime.agent import Agent\n"
        "class State(TypedDict):\n"
        "    value: str\n"
        "agent = Agent('loaded-twice', state=State)\n"
        "@agent.action('send_email')\n"
        "class SendEmail(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    to: str\n"
        "@agent.node(start=True)\n"
        "def draft(state):\n"
        "    return {}\n"
        "@agent.tool\n"
        "def look(pattern: str) -> str:\n"
        "    'Look.'\n"
        "    return pattern\n"
    )

    def load() -> Agent:
        spec = importlib.util.spec_from_file_location("defines_an_agent", module)
        assert spec is not None and spec.loader is not None
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        return loaded.agent

    first, second = load(), load()

    assert first is not second
    assert [d.name for d in first.definitions] == [d.name for d in second.definitions]
    assert first.definitions[0].payload is not second.definitions[0].payload
    assert list(first.shape().nodes) == list(second.shape().nodes) == ["draft"]
    assert first.tool_schemas() == second.tool_schemas()


# ----------------------------------------------------------------- the graph
def test_node_is_registered_under_its_name_and_stays_a_plain_function(agent):
    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {"value": "drafted"}

    @agent.node
    def send(state: State) -> dict[str, Any]:
        return {"value": "sent"}

    shape = agent.shape()
    assert list(shape.nodes) == ["draft", "send"]
    assert shape.start == "draft"
    assert shape.nodes["send"].function is send
    assert draft({"value": ""}) == {"value": "drafted"}


def test_edges_and_routes_are_named_by_the_nodes_they_connect(agent):
    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    @agent.node
    def send(state: State) -> dict[str, Any]:
        return {}

    @agent.node
    def give_up(state: State) -> dict[str, Any]:
        return {}

    agent.edge(draft, send)
    agent.edge(give_up, END)

    @agent.route(after=send, to=[give_up, END])
    def after_send(state: State) -> str:
        return END

    shape = agent.shape()
    assert [(edge.after, edge.then) for edge in shape.edges] == [
        ("draft", "send"),
        ("give_up", END),
    ]
    (route,) = shape.routes
    assert (route.after, route.targets) == ("send", ("give_up", END))


def test_edge_may_be_declared_before_the_nodes_it_connects(agent):
    def draft(state: State) -> dict[str, Any]:
        return {}

    def send(state: State) -> dict[str, Any]:
        return {}

    agent.edge(draft, send)
    agent.node(start=True)(draft)
    agent.node(send)

    assert [(edge.after, edge.then) for edge in agent.shape().edges] == [("draft", "send")]


def test_node_name_is_not_declared_twice(agent):
    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    def another(state: State) -> dict[str, Any]:
        return {}

    another.__name__ = "draft"

    with pytest.raises(InvalidAgent, match="two nodes named 'draft'"):
        agent.node(another)


def test_graph_has_one_start(agent):
    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    def send(state: State) -> dict[str, Any]:
        return {}

    with pytest.raises(InvalidAgent, match="two start nodes: 'draft' and 'send'"):
        agent.node(start=True)(send)

    assert list(agent.shape().nodes) == ["draft"]


def test_agent_without_nodes_has_no_shape(agent):
    with pytest.raises(InvalidAgent, match="no nodes"):
        agent.shape()


def test_agent_without_a_start_has_no_shape(agent):
    @agent.node
    def draft(state: State) -> dict[str, Any]:
        return {}

    with pytest.raises(InvalidAgent, match="no start node.*start=True"):
        agent.shape()


def test_edge_from_a_function_that_is_not_a_node_is_refused_when_the_shape_is_asked(agent):
    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    def stray(state: State) -> dict[str, Any]:
        return {}

    agent.edge(stray, draft)

    with pytest.raises(InvalidAgent, match="edge.*'stray'.*not a node"):
        agent.shape()


def test_edge_to_a_function_that_is_not_a_node_is_refused_when_the_shape_is_asked(agent):
    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    def stray(state: State) -> dict[str, Any]:
        return {}

    agent.edge(draft, stray)

    with pytest.raises(InvalidAgent, match="'stray'.*not a node"):
        agent.shape()


def test_edge_to_the_node_of_another_agent_with_the_same_name_is_refused(agent):
    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    @agent.node
    def send(state: State) -> dict[str, Any]:
        return {}

    def sent_elsewhere(state: State) -> dict[str, Any]:
        return {}

    sent_elsewhere.__name__ = "send"
    Agent("other-agent", state=State).node(start=True)(sent_elsewhere)
    agent.edge(draft, sent_elsewhere)

    with pytest.raises(InvalidAgent, match="'send'.*not a node of it"):
        agent.shape()


@pytest.mark.parametrize("end", ["draft", "send", ""])
def test_edge_to_text_that_is_not_the_end_is_refused(agent, end):
    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    with pytest.raises(InvalidAgent, match="node function or END"):
        agent.edge(draft, end)


def test_edge_cannot_start_at_the_end(agent):
    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    with pytest.raises(InvalidAgent, match="start of an edge"):
        agent.edge(END, draft)  # pyright: ignore[reportArgumentType]


def test_route_needs_somewhere_to_go(agent):
    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    with pytest.raises(InvalidAgent, match="somewhere to go"):
        agent.route(after=draft, to=[])


def test_route_to_a_function_that_is_not_a_node_is_refused_when_the_shape_is_asked(agent):
    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    def stray(state: State) -> dict[str, Any]:
        return {}

    @agent.route(after=draft, to=[stray, END])
    def after_draft(state: State) -> str:
        return END

    with pytest.raises(InvalidAgent, match="route.*'stray'.*not a node"):
        agent.shape()


def test_route_after_a_function_that_is_not_a_node_is_refused_when_the_shape_is_asked(agent):
    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    def stray(state: State) -> dict[str, Any]:
        return {}

    agent.route(after=stray, to=[END])(lambda state: END)

    with pytest.raises(InvalidAgent, match="route.*'stray'.*not a node"):
        agent.shape()


def test_route_answers_with_one_of_its_targets(agent):
    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    @agent.node
    def send(state: State) -> dict[str, Any]:
        return {}

    @agent.route(after=draft, to=[send, END])
    def after_draft(state: State) -> str:
        return "send" if state["value"] else END

    (route,) = agent.shape().routes
    assert route.choose({"value": "x"}) == "send"
    assert route.choose({"value": ""}) == END


@pytest.mark.parametrize("chosen", ["elsewhere", "draft", "", None, 7, ["send"]])
def test_route_that_chooses_what_is_not_one_of_its_targets_is_refused(agent, chosen):
    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    @agent.node
    def send(state: State) -> dict[str, Any]:
        return {}

    agent.route(after=draft, to=[send, END])(lambda state: chosen)

    (route,) = agent.shape().routes
    with pytest.raises(InvalidRouteChoice, match="after node 'draft'.*send, __end__"):
        route.choose({"value": ""})


def test_refusal_of_a_route_does_not_repeat_what_it_chose(agent):
    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    agent.route(after=draft, to=[END])(lambda state: SECRET)

    (route,) = agent.shape().routes
    with pytest.raises(InvalidRouteChoice) as refused:
        route.choose({"value": ""})
    assert SECRET not in str(refused.value)


def test_node_with_no_name_of_its_own_is_refused_because_two_would_be_one_name(agent):
    with pytest.raises(InvalidAgent, match="name of its own"):
        agent.node(lambda state: {})


def test_node_made_by_a_partial_has_no_name_of_its_own(agent):
    def draft(state: State, tone: str) -> dict[str, Any]:
        return {"value": tone}

    with pytest.raises(InvalidAgent, match="name of its own"):
        agent.node(functools.partial(draft, tone="warm"))


def test_node_has_one_route(agent):
    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    agent.route(after=draft, to=[END])(lambda state: END)
    agent.route(after=draft, to=[END])(lambda state: END)

    with pytest.raises(InvalidAgent, match="2 routes after node 'draft'"):
        agent.shape()


def test_shape_is_a_copy_that_what_is_declared_afterwards_does_not_reach(agent):
    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    shape = agent.shape()

    @agent.node
    def send(state: State) -> dict[str, Any]:
        return {}

    agent.edge(draft, send)

    assert list(shape.nodes) == ["draft"]
    assert shape.edges == ()
    assert list(agent.shape().nodes) == ["draft", "send"]


# ------------------------------------------------------------------ the run's context
@dataclass
class RunContext:
    greeting: str


@pytest.fixture
def contextual() -> Agent:
    return Agent("test-agent", state=State, context=RunContext)


def test_shape_says_the_types_the_graph_is_made_of(contextual, agent):
    @contextual.node(start=True)
    def draft(state: State, context: RunContext) -> dict[str, Any]:
        return {}

    @agent.node(start=True)
    def draft_alone(state: State) -> dict[str, Any]:
        return {}

    assert (contextual.shape().state, contextual.shape().context) == (State, RunContext)
    assert (agent.shape().state, agent.shape().context) == (State, None)


def test_context_of_an_agent_is_a_type(agent):
    with pytest.raises(InvalidAgent, match="context.*is a type"):
        Agent("test-agent", state=State, context="RunContext")  # pyright: ignore[reportArgumentType]


def test_node_takes_the_context_as_a_second_parameter(contextual):
    @contextual.node(start=True)
    def greet(state: State, context: RunContext) -> dict[str, Any]:
        return {"value": context.greeting}

    node = contextual.shape().nodes["greet"]

    assert node.takes_context
    assert node.run({"value": ""}, RunContext("hello")) == {"value": "hello"}


def test_node_of_an_agent_with_a_context_may_still_take_the_state_alone(contextual):
    @contextual.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {"value": "drafted"}

    node = contextual.shape().nodes["draft"]

    assert not node.takes_context
    assert node.run({"value": ""}, RunContext("unused")) == {"value": "drafted"}


def test_node_that_takes_a_context_is_refused_when_the_agent_declared_none(agent):
    def greet(state: State, context: RunContext) -> dict[str, Any]:
        return {}

    with pytest.raises(InvalidAgent, match="node 'greet'.*declared none"):
        agent.node(greet)

    assert agent.definitions == ()


def test_chooser_that_takes_a_context_is_refused_when_the_agent_declared_none(agent):
    @agent.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    with pytest.raises(InvalidAgent, match="chooser of a route.*declared none"):
        agent.route(after=draft, to=[END])(lambda state, context: END)


@pytest.mark.parametrize(
    "parameters",
    ["", "state, context, extra", "*states", "state, *, context", "**everything"],
    ids=["none", "three", "variadic", "keyword only", "keywords"],
)
def test_node_whose_parameters_are_not_the_state_and_the_context_is_refused(contextual, parameters):
    namespace: dict[str, Any] = {}
    exec(f"def draft({parameters}):\n    return {{}}\n", namespace)

    with pytest.raises(InvalidAgent, match="state and, as a second parameter, the agent's context"):
        contextual.node(namespace["draft"])


def test_chooser_takes_the_context_as_a_second_parameter(contextual):
    @contextual.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    @contextual.route(after=draft, to=[draft, END])
    def after_draft(state: State, context: RunContext) -> str:
        return "draft" if context.greeting else END

    (route,) = contextual.shape().routes
    assert route.choose({"value": ""}, RunContext("hello")) == "draft"
    assert route.choose({"value": ""}, RunContext("")) == END


def test_what_a_chooser_chose_is_checked_when_it_took_a_context(contextual):
    @contextual.node(start=True)
    def draft(state: State) -> dict[str, Any]:
        return {}

    contextual.route(after=draft, to=[END])(lambda state, context: SECRET)

    (route,) = contextual.shape().routes
    with pytest.raises(InvalidRouteChoice) as refused:
        route.choose({"value": ""}, RunContext("hello"))
    assert SECRET not in str(refused.value)


@pytest.mark.parametrize(
    "answer",
    [SECRET, ["value"], None, 7, ("value", SECRET)],
    ids=["text", "list", "none", "number", "tuple"],
)
def test_node_that_answers_with_something_other_than_a_mapping_is_refused_and_not_shown(
    agent, answer
):
    @agent.node(start=True)
    def draft(state: State) -> Any:
        return answer

    with pytest.raises(InvalidNodeResult, match="node 'draft'") as refused:
        agent.shape().nodes["draft"].run({"value": ""}, None)

    assert SECRET not in str(refused.value)
