"""Tests for ``Agent``: what an agent declares with decorators, and what is refused.

``Agent`` is framework-free, so nothing here builds a graph; ``test_langgraph_agent.py`` does.
A payload model carries the name of its action once, so a test that needs a model makes a new
one (``new_model``) instead of sharing a class between tests.
"""

import copy
import importlib.util
import uuid
from typing import Annotated, Any, TypedDict

import pytest
from pydantic import BaseModel, ConfigDict, Field

from openmarketer_core.agent_runtime.actions import (
    MAX_SHOWN_CHARACTERS,
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
    InvalidToolArguments,
    UnknownTool,
)
from openmarketer_core.agent_runtime.langgraph_steps import request_action
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
def test_action_name_travels_on_the_model_the_decorator_returns(agent):
    model = new_model()

    declared = agent.action("send_email")(model)

    assert declared is model
    assert action_name_of(model(to="ada")) == "send_email"


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


def test_action_that_says_nothing_of_its_limits_gets_the_defaults(agent):
    agent.action("send_email")(new_model())

    assert agent.policies["send_email"] == ActionPolicy(
        attempt_timeout_seconds=30, max_attempts=3, retry_after_seconds=1
    )


def test_limits_of_an_action_become_its_policy(agent):
    agent.action("render", timeout=600, retries=2, retry_after=5, heartbeat=60)(new_model())

    assert agent.policies["render"] == ActionPolicy(
        attempt_timeout_seconds=600,
        max_attempts=2,
        retry_after_seconds=5,
        heartbeat_timeout_seconds=60,
    )


@pytest.mark.parametrize(
    ("limit", "value"),
    [("timeout", 0), ("retries", 0), ("retry_after", 0), ("heartbeat", 0), ("heartbeat", -1)],
)
def test_limit_that_is_not_positive_is_refused_on_the_line_of_the_decorator(agent, limit, value):
    with pytest.raises(ValueError, match="more than 0"):
        agent.action("send_email", **{limit: value})


def test_policies_are_a_read_only_view(agent):
    agent.action("send_email")(new_model())

    with pytest.raises(TypeError):
        agent.policies["publish"] = ActionPolicy(1, 1, 1)  # pyright: ignore[reportIndexIssue]
    assert list(agent.policies) == ["send_email"]


def test_definitions_are_a_copy_that_does_not_change_the_agent(agent):
    agent.action("send_email")(new_model())

    assert isinstance(agent.definitions, tuple)
    assert len(agent.definitions) == 1


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


def test_model_that_was_never_declared_is_not_an_action():
    with pytest.raises(NotAnAction, match="SendEmail"):
        action_name_of(new_model()(to="ada"))


def test_refusal_of_a_model_that_was_never_declared_names_the_class_and_never_the_payload():
    with pytest.raises(NotAnAction) as refused:
        action_name_of(new_model()(to=SECRET))

    assert SECRET not in str(refused.value)


def test_node_that_asks_with_a_model_that_was_never_declared_is_refused_before_it_asks():
    with pytest.raises(NotAnAction) as refused:
        request_action(new_model()(to=SECRET))

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


def test_agent_needs_its_state_by_name():
    with pytest.raises(TypeError):
        Agent("test-agent", State)  # pyright: ignore[reportCallIssue]


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
    assert one.shape().nodes["draft"] is not other.shape().nodes["draft"]


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
    assert shape.nodes["send"] is send
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


# -------------------------------------------------------------------- tools
@pytest.fixture
def reading(agent) -> Agent:
    @agent.tool
    def read_file(path: str, start_line: int = 1) -> str:
        """Read a file with line numbers, at most 250 lines per call.

        Use 'start_line' to continue further down.
        """
        return f"{path}:{start_line}"

    return agent


def test_schema_of_a_tool_comes_from_its_type_hints_and_its_docstring(reading):
    assert reading.tool_schemas() == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    "Read a file with line numbers, at most 250 lines per call.\n\n"
                    "Use 'start_line' to continue further down."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer", "default": 1},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def test_parameter_is_described_through_its_annotation(agent):
    @agent.tool
    def look(pattern: Annotated[str, Field(description="a regular expression")]) -> str:
        """Look for a pattern."""
        return pattern

    (schema,) = agent.tool_schemas()
    assert schema["function"]["parameters"]["properties"] == {
        "pattern": {"type": "string", "description": "a regular expression"}
    }


def test_tool_without_parameters_takes_no_arguments(agent):
    @agent.tool
    def ping() -> str:
        """Check that the tools work."""
        return "pong"

    (schema,) = agent.tool_schemas()
    assert schema["function"]["parameters"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert agent.call_tool("ping", {}) == "pong"


def test_agent_without_tools_has_no_schemas(agent):
    assert agent.tool_schemas() == []


def test_schemas_are_copies_that_the_caller_may_change(reading):
    schemas = reading.tool_schemas()
    pristine = copy.deepcopy(schemas)
    schemas[0]["function"]["parameters"]["properties"].clear()
    schemas.clear()

    assert reading.tool_schemas() == pristine


def test_tool_stays_a_plain_function(agent):
    def look(pattern: str) -> str:
        """Look for a pattern."""
        return pattern

    assert agent.tool(look) is look


def test_tool_is_called_with_the_arguments_the_model_gave(reading):
    assert reading.call_tool("read_file", {"path": "README.md", "start_line": 5}) == "README.md:5"


def test_argument_left_out_has_the_default_of_the_function(reading):
    assert reading.call_tool("read_file", {"path": "README.md"}) == "README.md:1"


def test_model_that_a_tool_takes_arrives_as_a_model_and_not_as_a_dict(agent):
    class Span(BaseModel):
        model_config = ConfigDict(extra="forbid")

        first: int
        last: int

    @agent.tool
    def lines(span: Span) -> str:
        """Count the lines of a span."""
        return str(span.last - span.first + 1)

    assert agent.call_tool("lines", {"span": {"first": 3, "last": 5}}) == "3"


@pytest.mark.parametrize(
    ("arguments", "fields"),
    [
        ({"path": "a", "start_line": "3"}, ("start_line",)),
        ({"path": "a", "start_line": 3.0}, ("start_line",)),
        ({"path": "a", "start_line": True}, ("start_line",)),
        ({"path": 7}, ("path",)),
        ({"path": "a", "cc": "boss"}, ("cc",)),
        ({}, ("path",)),
    ],
    ids=[
        "text for a number",
        "float for an int",
        "bool for an int",
        "number for text",
        "extra",
        "missing",
    ],
)
def test_arguments_that_do_not_fit_are_refused_and_not_converted(reading, arguments, fields):
    with pytest.raises(InvalidToolArguments, match="read_file") as refused:
        reading.call_tool("read_file", arguments)

    assert refused.value.fields == fields


@pytest.mark.parametrize("value", [float("nan"), float("inf")], ids=["nan", "inf"])
def test_number_that_json_cannot_write_is_refused(agent, value):
    @agent.tool
    def scale(ratio: float) -> str:
        """Scale something."""
        return str(ratio)

    with pytest.raises(InvalidToolArguments) as refused:
        agent.call_tool("scale", {"ratio": value})

    assert refused.value.fields == ("ratio",)


def test_text_is_accepted_where_json_has_text_for_the_type(agent):
    @agent.tool
    def find(post_id: uuid.UUID) -> str:
        """Find a post."""
        return str(post_id)

    uuid_text = "5908bdbd-68bf-4c0b-bc64-7c881d190276"
    assert agent.call_tool("find", {"post_id": uuid_text}) == uuid_text


def test_refusal_of_arguments_names_the_fields_and_never_what_was_sent(reading):
    with pytest.raises(InvalidToolArguments) as refused:
        reading.call_tool("read_file", {"path": "a", "start_line": SECRET, "token": SECRET})

    assert SECRET not in str(refused.value)
    assert SECRET not in repr(refused.value.fields)
    assert refused.value.__cause__ is None
    assert refused.value.__suppress_context__


def test_argument_of_a_nested_model_that_the_model_did_not_declare_is_refused(agent):
    class Span(BaseModel):
        model_config = ConfigDict(extra="forbid")

        first: int

    @agent.tool
    def lines(span: Span) -> str:
        """Count the lines of a span."""
        return ""

    with pytest.raises(InvalidToolArguments) as refused:
        agent.call_tool("lines", {"span": {"first": 1, "last": SECRET}})

    assert refused.value.fields == ("span.last",)


def test_tool_that_the_agent_does_not_have_is_unknown(reading):
    with pytest.raises(UnknownTool, match="delete_everything") as refused:
        reading.call_tool("delete_everything", {})

    assert refused.value.tool_name == "delete_everything"


def test_name_of_an_unknown_tool_is_shortened_in_the_message(reading):
    with pytest.raises(UnknownTool) as refused:
        reading.call_tool("x" * 1000 + "\nforged log line", {})

    assert len(str(refused.value)) < 2 * MAX_SHOWN_CHARACTERS
    assert "\n" not in str(refused.value)


def test_tool_without_a_type_hint_is_refused_when_it_is_declared(agent):
    def look(pattern, glob: str = "*") -> str:  # pyright: ignore[reportMissingParameterType]
        """Look for a pattern."""
        return pattern

    with pytest.raises(InvalidAgent, match="parameter 'pattern' of tool 'look' has no type hint"):
        agent.tool(look)  # pyright: ignore[reportArgumentType]

    assert agent.tool_schemas() == []


def test_tool_without_a_docstring_is_refused_because_the_model_reads_it(agent):
    def look(pattern: str) -> str:
        return pattern

    with pytest.raises(InvalidAgent, match="tool 'look' has no docstring"):
        agent.tool(look)


@pytest.mark.parametrize(
    "parameters",
    ["*patterns: str", "**options: str", "pattern: str, /"],
    ids=["variadic", "keywords", "positional only"],
)
def test_tool_with_a_parameter_that_cannot_be_given_by_name_is_refused(agent, parameters):
    namespace: dict[str, Any] = {}
    exec(f'def look({parameters}) -> str:\n    """Look."""\n    return ""\n', namespace)

    with pytest.raises(InvalidAgent, match="cannot be given by name"):
        agent.tool(namespace["look"])


def test_tool_with_a_hint_that_cannot_be_read_is_refused(agent):
    def look(pattern: "NoSuchType") -> str:  # type: ignore[name-defined]  # noqa: F821
        """Look."""
        return ""

    with pytest.raises(InvalidAgent, match="cannot read the type hints of tool 'look'"):
        agent.tool(look)  # pyright: ignore[reportArgumentType]


def test_tool_that_takes_a_model_that_allows_extra_fields_is_refused(agent):
    class Lenient(BaseModel):
        value: str

    def look(inner: Lenient) -> str:
        """Look."""
        return ""

    with pytest.raises(InvalidAgent, match="Lenient.*forbid extra fields"):
        agent.tool(look)


def test_tool_name_is_not_declared_twice(reading):
    def read_file(path: str) -> str:
        """Another."""
        return path

    with pytest.raises(InvalidAgent, match="two tools named 'read_file'"):
        reading.tool(read_file)
