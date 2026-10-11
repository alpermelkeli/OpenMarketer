"""The actions an agent graph may ask for: the request, the outcome and the list of allowed ones.

A graph does no side effect itself. A node asks for an action by name with a
payload (``ActionRequest``), the workflow runs it, and the answer comes back as
an ``ActionOutcome``. Both are plain dataclasses of JSON data on purpose: the
workflow engine's default converter carries them between the activity that
runs the graph and the one that runs the action, and they are all its history
keeps of the work.

The name and the payload are text the model may have shaped, so they are data,
never a decision: ``ActionCatalog`` is the allowlist. An action that is not in
it does not run, and a payload that does not fit the action's model does not
run either. A payload is read as strict JSON: no value is converted to the
type the model wants (``"yes"`` is not true, ``"7"`` is not 7), nothing JSON
cannot write (NaN, infinity) passes, and every model of the payload forbids
fields it does not declare, so a misspelt or invented field is a refusal and
not a silent default. What the catalog says about a refusal names the action
and the fields at fault, never the values, which may hold a secret or an
instruction.

``request_digest`` says what a request is, so that the outcome of a request
is delivered only to a node that asks for the same thing again.

This module runs nothing and knows no workflow engine, graph framework or
database. Which activity runs an action, how often it is retried and whether a
person must approve it are the worker's.

The annotations of the dataclasses are real types, not text: a workflow decodes
their values in the workflow sandbox, whose data converter evaluates the
annotations of a class in the namespace of its module, and that module is not
always the one the sandbox has loaded. Hence no ``from __future__ import
annotations`` here or in ``steps.py``.
"""

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, get_args

from pydantic import BaseModel, ValidationError

# The most of a name, or of a list of names, an error message repeats of what the model sent.
MAX_SHOWN_CHARACTERS = 60
MAX_SHOWN_FIELDS = 5


class InvalidActionDefinition(ValueError):
    """An action cannot be defined as given, or the catalog would hold two of one name."""


class UnknownAction(Exception):
    """The request names an action that is not in the catalog."""

    def __init__(self, action_name: str) -> None:
        super().__init__(f"action {_shown(action_name)} is not one of the allowed actions")
        self.action_name = action_name


class InvalidActionPayload(Exception):
    """The payload of the request does not fit the model of its action."""

    def __init__(self, action_name: str, fields: tuple[str, ...]) -> None:
        shown = ", ".join(_shown(name) for name in fields[:MAX_SHOWN_FIELDS])
        more = len(fields) - MAX_SHOWN_FIELDS
        if more > 0:
            shown += f" and {more} more"
        detail = f": {shown}" if shown else ""
        super().__init__(f"payload of action {_shown(action_name)} does not fit{detail}")
        self.action_name = action_name
        self.fields = fields


@dataclass(frozen=True)
class ActionRequest:
    """What a node asks the workflow to do.

    ``action_id`` is the idempotency key of the work: ``<thread id>:<interrupt
    id>``. It is the same each time the same step is attempted again, so the
    action that runs it can tell a repeat from new work.
    """

    action_id: str
    name: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ActionOutcome:
    """What came of an action, as the graph is told.

    ``result`` is small JSON: a large output is stored elsewhere and
    referenced from here. ``error`` is worded for the graph, which decides what
    to do about a failure; it is empty when the action succeeded and says what
    went wrong when it did not.
    """

    action_id: str
    succeeded: bool
    result: dict[str, Any]
    error: str

    def __post_init__(self) -> None:
        if self.succeeded and self.error:
            raise ValueError("a successful outcome has no error")
        if not self.succeeded and not self.error:
            raise ValueError("a failed outcome must say what went wrong")


@dataclass(frozen=True)
class ActionDefinition:
    """An allowed action: its name and the model its payload must fit."""

    name: str
    payload: type[BaseModel]

    def __post_init__(self) -> None:
        # Without this a payload with a misspelled or invented field would pass unnoticed, and
        # the library's default for a nested model is to drop what it does not know.
        for model in _models_of(self.payload, set()):
            if model.model_config.get("extra") != "forbid":
                raise InvalidActionDefinition(
                    f"model {model.__name__} in the payload of action {self.name!r} "
                    "must forbid extra fields (model_config extra='forbid')"
                )


class ActionCatalog:
    """The actions a graph may ask for, by name; the allowlist the workflow checks requests with."""

    def __init__(self, definitions: Iterable[ActionDefinition]) -> None:
        by_name: dict[str, ActionDefinition] = {}
        for definition in definitions:
            if definition.name in by_name:
                raise InvalidActionDefinition(f"action {definition.name!r} is defined twice")
            by_name[definition.name] = definition
        self._by_name = by_name

    def validate(self, request: ActionRequest) -> BaseModel:
        """The payload of ``request`` as the model of its action.

        Raises ``UnknownAction`` when the catalog has no such action and
        ``InvalidActionPayload`` when the payload does not fit its model.
        """
        definition = self._by_name.get(request.name)
        if definition is None:
            raise UnknownAction(request.name)
        try:
            text = json.dumps(request.payload, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raise InvalidActionPayload(request.name, _fields_not_json(request.payload)) from None
        try:
            # Strict, and from JSON text: nothing is converted to the type the model wants, and
            # the library still reads a UUID, a date or an enum value from the string JSON has.
            return definition.payload.model_validate_json(text, strict=True)
        except ValidationError as e:
            # ``from None``: the library's error holds the values it refused.
            raise InvalidActionPayload(request.name, _fields_at_fault(e)) from None


def request_digest(name: str, payload: Mapping[str, Any]) -> str:
    """What a request is, as text: the same for the same action name and payload, and no other.

    The payload is read as JSON and written with sorted keys and no spaces, so
    the order its keys were built in does not matter. The graph adapter takes
    this digest of a request when it delivers the request's outcome, and a node
    takes it again of what it asks for when it runs again with that outcome.
    """
    canonical = json.dumps(
        {"name": name, "payload": payload}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _models_of(annotation: Any, seen: set[type[BaseModel]]) -> Iterator[type[BaseModel]]:
    """Each model that ``annotation`` is or holds, at any depth, once; ``seen`` stops a cycle."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation not in seen:
            seen.add(annotation)
            yield annotation
            for field in annotation.model_fields.values():
                yield from _models_of(field.annotation, seen)
        return
    for argument in get_args(annotation):
        yield from _models_of(argument, seen)


def _fields_not_json(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """The fields of a payload that JSON cannot hold, to name them without showing them."""
    not_json: list[str] = []
    for field, value in payload.items():
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            not_json.append(str(field))
    return tuple(not_json)


def _fields_at_fault(error: ValidationError) -> tuple[str, ...]:
    problems = error.errors(include_input=False, include_url=False, include_context=False)
    named = (".".join(str(part) for part in problem["loc"]) for problem in problems)
    # Several problems of one field are one field; an error of the whole model names none.
    return tuple(dict.fromkeys(name for name in named if name))


def _shown(text: str) -> str:
    """Text the model may have written, quoted on one line and short enough for a message."""
    quoted = repr(text)
    if len(quoted) <= MAX_SHOWN_CHARACTERS:
        return quoted
    return quoted[: MAX_SHOWN_CHARACTERS - 3] + "..."
