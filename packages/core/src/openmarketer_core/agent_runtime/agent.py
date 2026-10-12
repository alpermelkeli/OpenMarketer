"""An agent's parts, declared with decorators and kept as plain data.

An ``Agent`` is where the author of an agent says what it is: the actions its
graph may ask for (``action``), the nodes of the graph and how they follow one
another (``node``, ``edge``, ``route``), and the tools its model may call
(``tool``, kept by ``tools.Toolbox``). Each decorator only records. Nothing here
builds a graph, schedules an activity or calls a model: ``langgraph_agent.py``
turns the parts into a graph, and the worker turns the actions into activities.
That is why this module imports no graph framework, workflow engine or database,
and a workflow's module may load it.

The registries are plain data on the instance and never in the module, so two
agents share nothing and importing a module that defines an agent twice gives
two fresh ones. The one thing an agent writes outside itself is the name a
payload model carries (``actions.mark_as_action``).

An agent may declare a context type: what a run needs that is not the graph's state, such
as a model client or the repository snapshot of the attempt. It is built by the agent's
wiring for each attempt and is never stored with the state, so it may hold anything. A node,
a route's chooser or a tool takes it as one more parameter, and an agent that declared none
refuses a function that asks for it.

What cannot be right is refused where it is declared, or when the whole is
asked for (``shape``), with an error that names what is wrong. Only what code
cannot know before it runs is checked at run time: what a node answers with and the answer
of a route, and what a model wrote, which is read as strict JSON
(``model_input.read_strict_json``): an error says which fields are at fault, by names the
code declared, and never shows their values.

This module runs no node, action or tool of its own accord, and decides nothing
about who may ask for what: ``actions.ActionCatalog`` is the allowlist.
"""

import inspect
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, overload

from pydantic import BaseModel

from openmarketer_core.agent_runtime.actions import (
    ActionDefinition,
    ActionPolicy,
    InvalidActionDefinition,
    mark_as_action,
)
from openmarketer_core.agent_runtime.steps import InvalidNodeResult
from openmarketer_core.agent_runtime.threads import validate_kind
from openmarketer_core.agent_runtime.tools import InvalidTool, Toolbox, ToolFunction

# The end of the graph, as an edge or a route names it. It is the text LangGraph's own end is,
# so that a route's answer reaches the graph as it is; a test holds the two together.
END = "__end__"

# What an action is tried with when its declaration says nothing. A short call to a service
# fits 30 seconds, and an action that runs longer says so and sets a heartbeat. An action has a
# side effect, and nothing here can make a repeat of it safe: whether the service behind a
# handler treats a repeat as the same work is a fact about that service, which the framework
# cannot check. So an action is tried once, and trying again is a choice made for each action
# (``attempts``) by whoever knows its handler survives it. The wait before the second attempt,
# once there is one, is a second, and each wait doubles.
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_ATTEMPTS = 1
DEFAULT_RETRY_AFTER_SECONDS = 1

# A node reads the graph's state (and the agent's context, if it takes one) and answers with
# what it changes; a route's chooser reads the same and answers with the name of the node that
# follows, or END. The state and the context are the agent's own types, so these accept any.
NodeFunction = Callable[..., Mapping[str, Any]]
RouteChooser = Callable[..., str]


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


@dataclass(frozen=True)
class Edge:
    """After the node ``after`` comes the node ``then``, or the end of the graph."""

    after: str
    then: str


@dataclass(frozen=True)
class Node:
    """A node of the graph: its name, its function, and whether the function takes the context."""

    name: str
    function: NodeFunction
    takes_context: bool

    def run(self, state: Any, context: Any) -> Mapping[str, Any]:
        """The changes the node makes to the state.

        Raises ``InvalidNodeResult`` for anything but a mapping. The library reads some other
        answers as an instruction (a command that says which node runs next), which would let a
        node go where no edge or route of its agent allows.
        """
        changes = self.function(state, context) if self.takes_context else self.function(state)
        if not isinstance(changes, Mapping):
            raise InvalidNodeResult(self.name)
        return changes


@dataclass(frozen=True)
class Route:
    """After the node ``after`` comes the one of ``targets`` that ``chooser`` names."""

    after: str
    targets: tuple[str, ...]
    chooser: RouteChooser
    takes_context: bool

    def choose(self, state: Any, context: Any = None) -> str:
        """What the chooser says, if it is one of the targets.

        Raises ``InvalidRouteChoice`` otherwise: the chooser's answer is not known
        before it runs, and a graph must not go where its agent did not say it may.
        """
        chosen = self.chooser(state, context) if self.takes_context else self.chooser(state)
        if chosen not in self.targets:
            raise InvalidRouteChoice(self.after, self.targets)
        return chosen


@dataclass(frozen=True)
class GraphShape:
    """The graph of an agent, checked: its types, its nodes, where it starts, and what follows what.

    It is a copy: what an agent declares after asking for it is not in it.
    """

    state: type
    context: type | None
    nodes: Mapping[str, Node]
    start: str
    edges: tuple[Edge, ...]
    routes: tuple[Route, ...]


@dataclass(frozen=True)
class _DeclaredRoute:
    """A route as it was declared, with its ends as given: they are checked in ``shape``."""

    after: NodeFunction
    to: tuple[NodeFunction | str, ...]
    chooser: RouteChooser
    takes_context: bool


class Agent:
    """One agent: its kind, its graph's state, and the parts it declares with decorators."""

    def __init__(self, kind: str, *, state: type, context: type | None = None) -> None:
        """``kind`` names the agent's runs (``threads.validate_kind``); ``state`` is the graph's.

        ``context`` is the type of what the wiring builds for each attempt (see the module's
        docstring); it is left out by an agent whose functions need nothing but the state.
        Raises ``ValueError`` for a ``kind`` that cannot name a thread and ``InvalidAgent``
        for a ``context`` that is not a type.
        """
        validate_kind(kind)
        if context is not None and not isinstance(context, type):
            raise InvalidAgent(f"the context of agent {kind!r} is a type, not {context!r}")
        self.kind = kind
        self.state = state
        self.context = context
        self._definitions: dict[str, ActionDefinition] = {}
        self._policies: dict[str, ActionPolicy] = {}
        self._nodes: dict[str, Node] = {}
        self._start: str | None = None
        self._edges: list[tuple[NodeFunction, NodeFunction | str]] = []
        self._routes: list[_DeclaredRoute] = []
        self._tools = Toolbox(kind, context)

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
        attempts: int = DEFAULT_ATTEMPTS,
        retry_after: float = DEFAULT_RETRY_AFTER_SECONDS,
        heartbeat: float | None = None,
    ) -> Callable[[type[Payload]], type[Payload]]:
        """Declare the model it decorates as the payload of the action ``name``.

        ``timeout`` is the seconds one attempt may take. ``attempts`` counts
        the tries, the first included, as ``ActionPolicy.max_attempts`` does: the
        default is one, and more is for an action whose handler is safe to repeat.
        ``retry_after`` is the seconds before the second attempt, and
        ``heartbeat`` the heartbeat timeout of an action that runs long. A limit
        that is not finite and positive, or goes beyond what ``ActionPolicy`` allows,
        raises ``ValueError`` here, on the line of the decorator.

        The model is returned as it is, carrying the name, so that a node asks
        with ``request_action(Model(...))``. Raises ``InvalidActionDefinition``
        for a name this agent has, a model that is already an action's, a model (or one
        it holds) that allows extra fields, and a field that reads text as a file or an
        import.
        """
        policy = ActionPolicy(
            attempt_timeout_seconds=timeout,
            max_attempts=attempts,
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
    def node[Function: NodeFunction](self, function: Function) -> Function: ...

    @overload
    def node[Function: NodeFunction](
        self, *, start: bool = False
    ) -> Callable[[Function], Function]: ...

    def node[Function: NodeFunction](
        self, function: Function | None = None, *, start: bool = False
    ) -> Function | Callable[[Function], Function]:
        """Declare a function ``(state) -> changes`` as a node of the graph, named as it is.

        A node of an agent with a context takes it as a second parameter:
        ``(state, context) -> changes``. Used bare (``@agent.node``) or as
        ``@agent.node(start=True)`` for the node the graph starts at, of which there is
        exactly one. Raises ``InvalidAgent`` for a function that has no name of its own (a
        lambda), a name this agent already has, a second start, parameters other than
        the state and the context, and a context the agent did not declare.
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

        The chooser takes the state (and the context, as a node does) and answers with a
        node's name, or ``END``. What it answers is not known before it runs:
        ``Route.choose`` refuses anything that is not one of ``to``. A node has one route.
        """
        _require_node_function(after, "the start of a route")
        if not to:
            raise InvalidAgent("a route needs somewhere to go: give it targets")
        for target in to:
            _require_node_function_or_end(target, "a target of a route")

        def declare(chooser: Chooser) -> Chooser:
            takes_context = self._takes_context(chooser, "the chooser of a route")
            self._routes.append(_DeclaredRoute(after, tuple(to), chooser, takes_context))
            return chooser

        return declare

    def shape(self) -> GraphShape:
        """The graph of the agent, checked.

        Raises ``InvalidAgent`` for an agent with no node or no start, an edge or a route
        that names a function that is not a node of this agent (another agent's node with
        the same name is not one), and a node with two routes.
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
                takes_context=declared.takes_context,
            )
            for declared in self._routes
        )
        for after, count in Counter(route.after for route in routes).items():
            if count > 1:
                raise InvalidAgent(
                    f"agent {self.kind!r} has {count} routes after node {after!r}: "
                    "a node has one route, which chooses among everything that may follow it"
                )
        nodes = MappingProxyType(dict(self._nodes))
        return GraphShape(self.state, self.context, nodes, self._start, edges, routes)

    # -------------------------------------------------------------------- tools
    def tool[Tool: ToolFunction](self, function: Tool) -> Tool:
        """Declare a function the model may call, inside the graph's own nodes.

        What the model is told comes from the function: its docstring is the
        description, and its parameters, each with a type hint, are the
        arguments. They are read as strict JSON when the tool is called, and a
        model the arguments hold must forbid extra fields. A parameter with a
        default may be left out. The function answers with the text the model reads.

        A tool runs inline in the node that calls it and has no side effect of its own. It
        never asks for an action: a tool answers with text, and an action is asked for by a
        node, from what the model submitted, never from inside a tool the model chose to
        call (``request_action`` refuses it). A parameter named ``context``, annotated with
        the agent's context type, is the run's: the model is not told of it and is not
        asked for it.

        Raises ``InvalidAgent`` for a function with no docstring, a parameter with no
        type hint, one that cannot be given by name or cannot be a field (a name that starts
        with an underscore, or ``model_config``), a ``context`` that is not the agent's, a
        model that allows extra fields, a type that reads text as a file or an import, and a
        name the agent has already.
        """
        try:
            self._tools.add(function)
        except InvalidTool as e:
            raise InvalidAgent(str(e)) from None
        return function

    def tool_schemas(self) -> list[dict[str, Any]]:
        """The tools as the model is told of them: OpenAI-style function schemas."""
        return self._tools.schemas()

    def call_tool(self, name: str, arguments: Mapping[str, Any], context: Any = None) -> str:
        """Run the tool ``name`` with ``arguments``, the JSON the model gave, and give its answer.

        ``context`` is the run's, given to a tool that takes one. Raises ``UnknownTool``,
        ``InvalidToolArguments`` and ``InvalidToolAnswer`` as ``Toolbox.call`` does: the first
        two name the fields and never their values, and are for the node to tell the model.
        """
        return self._tools.call(name, arguments, context)

    # ---------------------------------------------------------------- internals
    def _declare_node[Function: NodeFunction](self, function: Function, *, start: bool) -> Function:
        name = _name_of(function)
        if name in self._nodes:
            raise InvalidAgent(f"agent {self.kind!r} has two nodes named {name!r}")
        if start and self._start is not None:
            raise InvalidAgent(
                f"agent {self.kind!r} has two start nodes: {self._start!r} and {name!r}"
            )
        takes_context = self._takes_context(function, f"node {name!r}")
        self._nodes[name] = Node(name, function, takes_context)
        if start:
            self._start = name
        return function

    def _takes_context(self, function: Callable[..., Any], what: str) -> bool:
        """Whether ``function`` takes the context as a second parameter, from its signature."""
        try:
            parameters = list(inspect.signature(function).parameters.values())
        except (TypeError, ValueError):
            raise InvalidAgent(f"cannot read the parameters of {what}") from None
        positional = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        if not 1 <= len(parameters) <= 2 or any(p.kind not in positional for p in parameters):
            raise InvalidAgent(
                f"{what} takes the graph's state and, as a second parameter, the agent's context "
                "if the agent has one: nothing else"
            )
        if len(parameters) == 2 and self.context is None:
            raise InvalidAgent(
                f"{what} takes a second parameter, the agent's context, and agent {self.kind!r} "
                "declared none: give Agent(..., context=) a type"
            )
        return len(parameters) == 2

    def _node_named(self, function: NodeFunction, where: str) -> str:
        name = _name_of(function)
        declared = self._nodes.get(name)
        if declared is None or declared.function is not function:
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
    if not isinstance(name, str) or not name.isidentifier():
        raise InvalidAgent(
            "a node is a function declared with def, which has a name of its own: "
            "a lambda or a partial has none, and two would be one name"
        )
    return name


def _require_node_function(function: object, role: str) -> None:
    if not callable(function):
        raise InvalidAgent(f"{role} is a node function, not {type(function).__name__}")


def _require_node_function_or_end(end: object, role: str) -> None:
    if end != END and not callable(end):
        raise InvalidAgent(f"{role} is a node function or END, not {type(end).__name__}")
