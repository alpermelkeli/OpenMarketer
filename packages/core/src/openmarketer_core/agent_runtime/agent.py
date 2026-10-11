"""An agent's parts, declared with decorators and kept as plain data.

An ``Agent`` is where the author of an agent says what it is: the actions its
graph may ask for (``action``), the nodes of the graph and how they follow one
another (``node``, ``edge``, ``route``), and the tools its model may call
(``tool``). Each decorator only records. Nothing here builds a graph, schedules
an activity or calls a model: ``langgraph_agent.py`` turns the parts into a
graph, and the worker turns the actions into activities. That is why this
module imports no graph framework, workflow engine or database, and a workflow's
module may load it.

The registries are plain data on the instance and never in the module, so two
agents share nothing and importing a module that defines an agent twice gives
two fresh ones. The one thing an agent writes outside itself is the name a
payload model carries (``actions.mark_as_action``).

What cannot be right is refused where it is declared, or when the whole is
asked for (``shape``), with an error that names what is wrong. Only what code
cannot know before it runs is checked at run time: the answer of a route, and
the arguments of a tool, which are text a model wrote and are read as strict JSON
(``actions.read_strict_json``): an error says which fields are at fault and never
shows their values.

This module runs no node, action or tool of its own accord, and decides nothing
about who may ask for what: ``actions.ActionCatalog`` is the allowlist.
"""

import copy
import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, get_type_hints, overload

from pydantic import BaseModel, ConfigDict, create_model
from pydantic.json_schema import GenerateJsonSchema

from openmarketer_core.agent_runtime.actions import (
    ActionDefinition,
    ActionPolicy,
    DoesNotFit,
    InvalidActionDefinition,
    fields_phrase,
    lenient_models,
    mark_as_action,
    read_strict_json,
    shown,
)
from openmarketer_core.agent_runtime.threads import validate_kind

# The end of the graph, as an edge or a route names it. It is the text LangGraph's own end is,
# so that a route's answer reaches the graph as it is; a test holds the two together.
END = "__end__"

# What an action is tried with when its declaration says nothing. A short call to a service
# fits 30 seconds, and an action that runs longer says so and sets a heartbeat. An action has a
# side effect, so three attempts (the first and two repeats) ride out a passing failure without
# trying often at something that does not pass; the handler's idempotency key makes a repeat
# safe. The wait before the second attempt is a second, and each wait doubles.
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_ATTEMPTS = 3
DEFAULT_RETRY_AFTER_SECONDS = 1

# A node reads the graph's state and answers with what it changes; a route's chooser reads the
# state and answers with the name of the node that follows, or END. The state is the agent's own
# type, so these accept any.
NodeFunction = Callable[[Any], Mapping[str, Any]]
RouteChooser = Callable[[Any], str]
# A tool answers with the text the model reads.
ToolFunction = Callable[..., str]


class InvalidAgent(ValueError):
    """The parts of an agent do not fit together: a name taken twice, an edge to nowhere."""


class InvalidRouteChoice(Exception):
    """A route chose something that is not one of its targets."""

    def __init__(self, after: str, targets: tuple[str, ...]) -> None:
        # What was chosen is not shown: it may come from the graph's state, which holds what a model
        # or a repository said.
        super().__init__(
            f"the route after node {after!r} chose something that is not one of "
            f"its targets: {', '.join(targets)}"
        )
        self.after = after
        self.targets = targets


class UnknownTool(Exception):
    """The model called a tool the agent does not have."""

    def __init__(self, tool_name: str) -> None:
        super().__init__(f"tool {shown(tool_name)} is not one of the agent's tools")
        self.tool_name = tool_name


class InvalidToolArguments(Exception):
    """The arguments the model gave a tool do not fit what the tool takes."""

    def __init__(self, tool_name: str, fields: tuple[str, ...]) -> None:
        super().__init__(f"arguments of tool {shown(tool_name)} do not fit{fields_phrase(fields)}")
        self.tool_name = tool_name
        self.fields = fields


@dataclass(frozen=True)
class Edge:
    """After the node ``after`` comes the node ``then``, or the end of the graph."""

    after: str
    then: str


@dataclass(frozen=True)
class Route:
    """After the node ``after`` comes the one of ``targets`` that ``chooser`` names."""

    after: str
    targets: tuple[str, ...]
    chooser: RouteChooser

    def choose(self, state: Any) -> str:
        """What the chooser says, if it is one of the targets.

        Raises ``InvalidRouteChoice`` otherwise: the chooser's answer is not known
        before it runs, and a graph must not go where its agent did not say it may.
        """
        chosen = self.chooser(state)
        if chosen not in self.targets:
            raise InvalidRouteChoice(self.after, self.targets)
        return chosen


@dataclass(frozen=True)
class GraphShape:
    """The graph of an agent, checked: its nodes, where it starts, and what follows what."""

    nodes: Mapping[str, NodeFunction]
    start: str
    edges: tuple[Edge, ...]
    routes: tuple[Route, ...]


@dataclass(frozen=True)
class _DeclaredRoute:
    """A route as it was declared, with its ends as given: they are checked in ``shape``."""

    after: NodeFunction
    to: tuple[NodeFunction | str, ...]
    chooser: RouteChooser


@dataclass(frozen=True)
class _ToolSpec:
    name: str
    description: str
    arguments: type[BaseModel]
    parameters: dict[str, Any]  # the JSON schema of ``arguments``
    function: ToolFunction


class Agent:
    """One agent: its kind, its graph's state, and the parts it declares with decorators."""

    def __init__(self, kind: str, *, state: type) -> None:
        """``kind`` names the agent's runs (``threads.validate_kind``); ``state`` is the graph's.

        Raises ``ValueError`` for a ``kind`` that cannot name a thread.
        """
        validate_kind(kind)
        self.kind = kind
        self.state = state
        self._definitions: dict[str, ActionDefinition] = {}
        self._policies: dict[str, ActionPolicy] = {}
        self._nodes: dict[str, NodeFunction] = {}
        self._start: str | None = None
        self._edges: list[tuple[NodeFunction, NodeFunction | str]] = []
        self._routes: list[_DeclaredRoute] = []
        self._tools: dict[str, _ToolSpec] = {}

    # ------------------------------------------------------------------ actions
    @property
    def definitions(self) -> tuple[ActionDefinition, ...]:
        """The actions the agent's graph may ask for, in the order they were declared."""
        return tuple(self._definitions.values())

    @property
    def policies(self) -> Mapping[str, ActionPolicy]:
        """How each action is tried, by action name."""
        return MappingProxyType(self._policies)

    def action[Payload: BaseModel](
        self,
        name: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        retries: int = DEFAULT_ATTEMPTS,
        retry_after: float = DEFAULT_RETRY_AFTER_SECONDS,
        heartbeat: float | None = None,
    ) -> Callable[[type[Payload]], type[Payload]]:
        """Declare the model it decorates as the payload of the action ``name``.

        ``timeout`` is the seconds one attempt may take. ``retries`` counts
        attempts, the first included, as ``ActionPolicy.max_attempts`` does.
        ``retry_after`` is the seconds before the second attempt, and
        ``heartbeat`` the heartbeat timeout of an action that runs long. A limit that
        is not positive raises ``ValueError`` here, on the line of the decorator.

        The model is returned as it is, carrying the name, so that a node asks
        with ``request_action(Model(...))``. Raises ``InvalidActionDefinition``
        for a name this agent has, a model that is already an action's, or a
        model (or one it holds) that allows extra fields.
        """
        policy = ActionPolicy(
            attempt_timeout_seconds=timeout,
            max_attempts=retries,
            retry_after_seconds=retry_after,
            heartbeat_timeout_seconds=heartbeat,
        )

        def declare(model: type[Payload]) -> type[Payload]:
            definition = ActionDefinition(name, model)
            if name in self._definitions:
                raise InvalidActionDefinition(f"agent {self.kind!r} defines action {name!r} twice")
            mark_as_action(model, name)
            self._definitions[name] = definition
            self._policies[name] = policy
            return model

        return declare

    # -------------------------------------------------------------------- graph
    @overload
    def node[Node: NodeFunction](self, function: Node) -> Node: ...

    @overload
    def node[Node: NodeFunction](self, *, start: bool = False) -> Callable[[Node], Node]: ...

    def node[Node: NodeFunction](
        self, function: Node | None = None, *, start: bool = False
    ) -> Node | Callable[[Node], Node]:
        """Declare a function ``(state) -> changes`` as a node of the graph, named as it is.

        Used bare (``@agent.node``) or as ``@agent.node(start=True)`` for the node
        the graph starts at, of which there is exactly one. Raises
        ``InvalidAgent`` for a name this agent already has and for a second start.
        """
        if function is None:
            return lambda declared: self._declare_node(declared, start=start)
        return self._declare_node(function, start=start)

    def edge(self, after: NodeFunction, then: NodeFunction | str) -> None:
        """After the node ``after`` comes the node ``then``, or ``END``.

        Both are checked to be nodes of this agent when ``shape`` is asked, so
        the order the parts are declared in does not matter.
        """
        _require_node_function(after, "the start of an edge")
        _require_node_function_or_end(then, "the end of an edge")
        self._edges.append((after, then))

    def route[Chooser: RouteChooser](
        self, *, after: NodeFunction, to: Sequence[NodeFunction | str]
    ) -> Callable[[Chooser], Chooser]:
        """After the node ``after`` comes the node, among ``to``, that the chooser names.

        The chooser takes the state and answers with a node's name, or ``END``.
        What it answers is not known before it runs: ``Route.choose`` refuses
        anything that is not one of ``to``.
        """
        _require_node_function(after, "the start of a route")
        if not to:
            raise InvalidAgent("a route needs somewhere to go: give it targets")
        for target in to:
            _require_node_function_or_end(target, "a target of a route")

        def declare(chooser: Chooser) -> Chooser:
            self._routes.append(_DeclaredRoute(after, tuple(to), chooser))
            return chooser

        return declare

    def shape(self) -> GraphShape:
        """The graph of the agent, checked.

        Raises ``InvalidAgent`` for an agent with no node or no start, and for an
        edge or a route that names a function that is not a node of this agent
        (another agent's node with the same name is not one).
        """
        if not self._nodes:
            raise InvalidAgent(f"agent {self.kind!r} has no nodes")
        if self._start is None:
            raise InvalidAgent(
                f"agent {self.kind!r} has no start node: declare one with @agent.node(start=True)"
            )
        edges = tuple(
            Edge(self._node_named(after, "an edge"), self._node_or_end_named(then, "an edge"))
            for after, then in self._edges
        )
        routes = tuple(
            Route(
                after=self._node_named(declared.after, "a route"),
                targets=tuple(self._node_or_end_named(t, "a route") for t in declared.to),
                chooser=declared.chooser,
            )
            for declared in self._routes
        )
        return GraphShape(MappingProxyType(dict(self._nodes)), self._start, edges, routes)

    # -------------------------------------------------------------------- tools
    def tool[Tool: ToolFunction](self, function: Tool) -> Tool:
        """Declare a function the model may call, inside the graph's own nodes.

        What the model is told comes from the function: its docstring is the
        description, and its parameters, each with a type hint, are the
        arguments. They are read as strict JSON when the tool is called, and a
        model the arguments hold must forbid extra fields. A parameter with a
        default may be left out. The function answers with the text the model reads.

        A tool runs inline in the node that calls it, so it has no side effect of
        its own. One that needs the outside world asks for an action with
        ``request_action`` from its body, and that is the one action its node
        may ask for.

        Raises ``InvalidAgent`` for a function with no docstring, a parameter with no
        type hint or one that cannot be given by name, a model that allows extra fields, and
        a name the agent has already.
        """
        spec = _tool_spec(function)
        if spec.name in self._tools:
            raise InvalidAgent(f"agent {self.kind!r} has two tools named {spec.name!r}")
        self._tools[spec.name] = spec
        return function

    def tool_schemas(self) -> list[dict[str, Any]]:
        """The tools as the model is told of them: OpenAI-style function schemas."""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": copy.deepcopy(spec.parameters),
                },
            }
            for spec in self._tools.values()
        ]

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> str:
        """Run the tool ``name`` with ``arguments``, the JSON the model gave, and give its answer.

        Raises ``UnknownTool`` for a tool the agent does not have and
        ``InvalidToolArguments`` for arguments that do not fit; the second names
        the fields and never their values.
        """
        spec = self._tools.get(name)
        if spec is None:
            raise UnknownTool(name)
        try:
            read = read_strict_json(spec.arguments, arguments)
        except DoesNotFit as e:
            raise InvalidToolArguments(name, e.fields) from None
        # Not ``model_dump``: it would turn a model the tool asks for into a dict.
        return spec.function(**{field: getattr(read, field) for field in type(read).model_fields})

    # ---------------------------------------------------------------- internals
    def _declare_node[Node: NodeFunction](self, function: Node, *, start: bool) -> Node:
        name = _name_of(function)
        if name in self._nodes:
            raise InvalidAgent(f"agent {self.kind!r} has two nodes named {name!r}")
        if start and self._start is not None:
            raise InvalidAgent(
                f"agent {self.kind!r} has two start nodes: {self._start!r} and {name!r}"
            )
        self._nodes[name] = function
        if start:
            self._start = name
        return function

    def _node_named(self, function: NodeFunction, where: str) -> str:
        name = _name_of(function)
        if self._nodes.get(name) is not function:
            raise InvalidAgent(
                f"{where} of agent {self.kind!r} names {name!r}, which is not a node of it: "
                "declare it with @agent.node"
            )
        return name

    def _node_or_end_named(self, end: NodeFunction | str, where: str) -> str:
        if isinstance(end, str):
            return end
        return self._node_named(end, where)


def _name_of(function: NodeFunction) -> str:
    name = getattr(function, "__name__", None)
    if not isinstance(name, str):
        raise InvalidAgent("a node is a plain function, which has a name")
    return name


def _require_node_function(end: object, role: str) -> None:
    if not callable(end):
        raise InvalidAgent(f"{role} is a node function, not {type(end).__name__}")


def _require_node_function_or_end(end: object, role: str) -> None:
    if end != END and not callable(end):
        raise InvalidAgent(f"{role} is a node function or END, not {type(end).__name__}")


class _TerseSchema(GenerateJsonSchema):
    """A JSON schema without the title the library makes of every field's name.

    The model reads the schema on every call, and a title only says again what the name does.
    """

    def field_title_should_be_set(self, schema: Any) -> bool:
        return False


def _tool_spec(function: ToolFunction) -> _ToolSpec:
    name = function.__name__
    description = inspect.getdoc(function)
    if not description:
        raise InvalidAgent(
            f"tool {name!r} has no docstring: the model reads it to decide when to call the tool"
        )
    arguments = _arguments_model(function)
    lenient = next(lenient_models(arguments), None)
    if lenient is not None:
        raise InvalidAgent(
            f"model {lenient.__name__} in the arguments of tool {name!r} "
            "must forbid extra fields (model_config extra='forbid')"
        )
    parameters = arguments.model_json_schema(schema_generator=_TerseSchema)
    parameters.pop("title", None)
    return _ToolSpec(name, description, arguments, parameters, function)


def _arguments_model(function: ToolFunction) -> type[BaseModel]:
    """A model with one field per parameter of ``function``, which forbids any other."""
    name = function.__name__
    try:
        hints = get_type_hints(function, include_extras=True)
    except (NameError, TypeError, AttributeError) as e:
        raise InvalidAgent(f"cannot read the type hints of tool {name!r}: {e}") from None
    fields: dict[str, Any] = {}
    for parameter in inspect.signature(function).parameters.values():
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            raise InvalidAgent(
                f"parameter {parameter.name!r} of tool {name!r} cannot be given by name: "
                "a tool takes the arguments the model names"
            )
        if parameter.name not in hints:
            raise InvalidAgent(
                f"parameter {parameter.name!r} of tool {name!r} has no type hint: "
                "the model's arguments are read as that type"
            )
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        fields[parameter.name] = (hints[parameter.name], default)
    return create_model(f"{name}_arguments", __config__=ConfigDict(extra="forbid"), **fields)
