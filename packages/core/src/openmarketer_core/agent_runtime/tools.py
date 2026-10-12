"""The tools of an agent: what its model may call, read from plain functions.

A tool is a function the model may ask a node to run in the middle of its work: look at a
file, search the repository. What the model is told of it comes from the function (its
docstring is the description, its type-hinted parameters are the arguments), and what the
model sends is read as strict JSON into those parameters (``model_input.read_strict_json``).
``Toolbox`` keeps the tools of one agent, tells the model of them (``schemas``) and runs
one (``call``).

A tool answers with text and nothing else, and has no side effect of its own. It never asks
for an action: an action is asked for by a node, from what the model submitted, and never
from inside a tool the model chose to call, because the model would then pick the action and
its arguments and nothing would stand between its words and the outside world (``call``
marks the tool as running, and ``running_tool`` is what ``request_action`` asks).

A parameter named ``context``, annotated with the agent's context type, is the run's and not
the model's: it is left out of what the model is told and is given at call time, so a tool
reaches the repository snapshot of the attempt it runs in.

This module imports no graph framework, workflow engine or database. It never runs a tool of
its own accord and does not decide when the model may call one: that is the node's.
"""

import copy
import inspect
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, get_type_hints

from pydantic import BaseModel, ConfigDict, create_model
from pydantic.json_schema import GenerateJsonSchema

from openmarketer_core.agent_runtime.model_input import (
    DoesNotFit,
    UnreadableModel,
    fields_phrase,
    fields_that_name_files_or_code,
    lenient_types,
    read_strict_json,
    shown,
)
from openmarketer_core.agent_runtime.steps import InvalidToolAnswer

# A tool answers with the text the model reads.
ToolFunction = Callable[..., str]

# The name of the one parameter that is the run's and not the model's.
CONTEXT_PARAMETER = "context"

# What the name of a tool the model called is shown as when it was not even text.
_NOT_TEXT = "<not text>"

# The tool that is running in this context, if any. The library runs a node in a context of
# its own and a tool runs inline in it, so what is set while a tool runs is seen by what the
# tool calls and by nothing else.
_running_tool: ContextVar[str | None] = ContextVar("running_tool", default=None)


class InvalidTool(ValueError):
    """A function cannot be a tool as it is declared, or the agent would hold two of one name."""


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


def running_tool() -> str | None:
    """The name of the tool that is running in this context, or ``None`` when none is."""
    return _running_tool.get()


@dataclass(frozen=True)
class _ToolSpec:
    name: str
    description: str
    arguments: type[BaseModel]
    parameters: dict[str, Any]  # the JSON schema of ``arguments``
    function: ToolFunction
    takes_context: bool


class _TerseSchema(GenerateJsonSchema):
    """A JSON schema without the title the library makes of every field's name.

    The model reads the schema on every call, and a title only says again what the name does.
    """

    def field_title_should_be_set(self, schema: Any) -> bool:
        return False


class Toolbox:
    """The tools of one agent, by name, in the order they were declared."""

    def __init__(self, agent_kind: str, context: type | None) -> None:
        """``context`` is the agent's context type, which a tool may take."""
        self._agent_kind = agent_kind
        self._context = context
        self._tools: dict[str, _ToolSpec] = {}

    def add(self, function: ToolFunction) -> None:
        """Declare ``function`` as a tool, named as it is.

        Raises ``InvalidTool`` for a function with no docstring, a parameter with no type
        hint, one that cannot be given by name or be a field of the arguments, a model or
        a type that reads text as a file or an import that the arguments hold, and a name the
        agent has already.
        """
        spec = self._spec(function)
        if spec.name in self._tools:
            raise InvalidTool(f"agent {self._agent_kind!r} has two tools named {spec.name!r}")
        self._tools[spec.name] = spec

    def schemas(self) -> list[dict[str, Any]]:
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

    def call(self, name: str, arguments: Mapping[str, Any], context: Any) -> str:
        """Run the tool ``name`` with ``arguments``, the JSON the model gave, and give its answer.

        ``context`` is given to a tool that takes one. Raises ``UnknownTool`` for a name that
        is not a tool's (including one that is not text) and ``InvalidToolArguments`` for
        arguments that do not fit (including ones that are not an object); the second names
        the fields and never their values. Raises ``InvalidToolAnswer`` for a tool that did
        not answer with text, and ``TypeError`` when the tool takes a context and none was given.
        """
        spec = self._tools.get(name) if isinstance(name, str) else None
        if spec is None:
            raise UnknownTool(name if isinstance(name, str) else _NOT_TEXT)
        if not isinstance(arguments, Mapping):
            raise InvalidToolArguments(spec.name, ())
        try:
            read = read_strict_json(spec.arguments, arguments)
        except DoesNotFit as e:
            raise InvalidToolArguments(spec.name, e.fields) from None
        # Not ``model_dump``: it would turn a model the tool asks for into a dict.
        given = {field: getattr(read, field) for field in type(read).model_fields}
        if spec.takes_context:
            if context is None:
                raise TypeError(f"tool {spec.name!r} takes the agent's context: give it to call")
            given[CONTEXT_PARAMETER] = context
        token = _running_tool.set(spec.name)
        try:
            answer = spec.function(**given)
        finally:
            _running_tool.reset(token)
        if not isinstance(answer, str):
            raise InvalidToolAnswer(spec.name, type(answer).__name__)
        return answer

    def _spec(self, function: ToolFunction) -> _ToolSpec:
        name = function.__name__
        description = inspect.getdoc(function)
        if not description:
            raise InvalidTool(
                f"tool {name!r} has no docstring: "
                "the model reads it to decide when to call the tool"
            )
        arguments, takes_context = self._arguments_model(function)
        try:
            lenient = next(lenient_types(arguments), None)
        except UnreadableModel as e:
            raise InvalidTool(f"arguments of tool {name!r}: {e}") from None
        if lenient is not None:
            raise InvalidTool(
                f"{lenient.__name__} in the arguments of tool {name!r} "
                "must forbid extra fields (extra='forbid' in its config)"
            )
        named = next(fields_that_name_files_or_code(arguments), None)
        if named is not None:
            structure, field = named
            where = (
                f"parameter {field!r} of tool {name!r}"
                if structure is arguments
                else f"field {field!r} of {structure.__name__} in the arguments of tool {name!r}"
            )
            raise InvalidTool(
                f"{where} reads text a model wrote as a file path or an import name: "
                "take it as a string and decide in the tool what it may name"
            )
        parameters = arguments.model_json_schema(schema_generator=_TerseSchema)
        parameters.pop("title", None)
        return _ToolSpec(name, description, arguments, parameters, function, takes_context)

    def _arguments_model(self, function: ToolFunction) -> tuple[type[BaseModel], bool]:
        """A model with one field per parameter the model gives, which forbids any other.

        With whether the function also takes the agent's context.
        """
        name = function.__name__
        try:
            hints = get_type_hints(function, include_extras=True)
        except (NameError, TypeError, AttributeError) as e:
            raise InvalidTool(f"cannot read the type hints of tool {name!r}: {e}") from None
        fields: dict[str, Any] = {}
        takes_context = False
        for parameter in inspect.signature(function).parameters.values():
            if parameter.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                raise InvalidTool(
                    f"parameter {parameter.name!r} of tool {name!r} cannot be given by name: "
                    "a tool takes the arguments the model names"
                )
            if parameter.name == CONTEXT_PARAMETER:
                self._require_context_annotation(name, hints)
                takes_context = True
                continue
            if parameter.name.startswith("_") or parameter.name == "model_config":
                raise InvalidTool(
                    f"parameter {parameter.name!r} of tool {name!r} cannot be a field of its "
                    "arguments: rename it, as a name that starts with an underscore, or "
                    "model_config, is not one the model could give"
                )
            if parameter.name not in hints:
                raise InvalidTool(
                    f"parameter {parameter.name!r} of tool {name!r} has no type hint: "
                    "the model's arguments are read as that type"
                )
            default = ... if parameter.default is inspect.Parameter.empty else parameter.default
            fields[parameter.name] = (hints[parameter.name], default)
        model = create_model(f"{name}_arguments", __config__=ConfigDict(extra="forbid"), **fields)
        return model, takes_context

    def _require_context_annotation(self, tool_name: str, hints: Mapping[str, Any]) -> None:
        if self._context is None:
            raise InvalidTool(
                f"parameter {CONTEXT_PARAMETER!r} of tool {tool_name!r} is the agent's context, "
                f"which agent {self._agent_kind!r} declared none of: give Agent(..., context=) "
                "a type, or rename the parameter"
            )
        if hints.get(CONTEXT_PARAMETER) is not self._context:
            raise InvalidTool(
                f"parameter {CONTEXT_PARAMETER!r} of tool {tool_name!r} must be annotated "
                f"{self._context.__name__}, the agent's context type: the name is the run's, "
                "and the model never gives it"
            )
