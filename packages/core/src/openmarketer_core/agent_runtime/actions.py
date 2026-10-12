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
run either. A payload is read as strict JSON (``model_input.read_strict_json``):
no value is converted to the type the model wants, nothing JSON cannot write passes,
and every model of the payload forbids fields it does not declare and names no
file or import, so a misspelt or invented field is a refusal and not a silent default.
What the catalog says about a refusal names the action and the fields at fault, never
the values, which may hold a secret or an instruction, nor a field name the code did
not declare.

A payload model carries the name of its action (``mark_as_action``, which an
agent's ``action`` decorator calls), so that a node asks with the payload alone
(``action_name_of``) and cannot misspell the name. ``ActionPolicy`` is how an
action is tried: its timeout and attempts, within ceilings.

``request_digest`` says what a request is, so that the outcome of a request
is delivered only to a node that asks for the same thing again.

This module runs nothing and knows no workflow engine, graph framework or
database. Which activity runs an action, how the policy is applied and whether a
person must approve it are the worker's.

The annotations of the dataclasses are real types, not text: a workflow decodes
their values in the workflow sandbox, whose data converter evaluates the
annotations of a class in the namespace of its module, and that module is not
always the one the sandbox has loaded. Hence no ``from __future__ import
annotations`` here or in ``steps.py``.
"""

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from openmarketer_core.agent_runtime.model_input import (
    DoesNotFit,
    UnreadableModel,
    fields_phrase,
    fields_that_name_files_or_code,
    lenient_types,
    read_strict_json,
    shown,
)

# What no action's policy goes beyond. Waits double from the first, so ten attempts already
# put a long way between the first and the last, and an action that still fails after that
# is better ended as an outcome the graph handles than tried again. A day is longer than
# anything one attempt of an action should hold a worker for (a longer job is several actions,
# or one with a heartbeat that resumes), and an hour is longer than any wait that rides out a
# passing failure. They also keep what is made of them far from what a duration cannot hold.
MAX_ATTEMPTS = 10
MAX_ATTEMPT_TIMEOUT_SECONDS = 24 * 60 * 60
MAX_RETRY_AFTER_SECONDS = 60 * 60

# Where a payload model keeps the name of its action. A dunder cannot be a field of a model, so it
# never clashes with one; it is read from the class itself, so a subclass is not an action unless
# it is registered.
_ACTION_NAME = "__action_name__"


class InvalidActionDefinition(ValueError):
    """An action cannot be defined as given, or the catalog would hold two of one name."""


class UnknownAction(Exception):
    """The request names an action that is not in the catalog."""

    def __init__(self, action_name: str) -> None:
        super().__init__(f"action {shown(action_name)} is not one of the allowed actions")
        self.action_name = action_name


class NotAnAction(Exception):
    """A payload model that no agent registered as an action asked for something."""

    def __init__(self, model: type[BaseModel]) -> None:
        # The model's class, never the payload: the payload may hold what the model must not echo.
        super().__init__(
            f"model {model.__name__} is not the payload of any action: "
            "register it with the agent's action decorator"
        )
        self.model = model


class InvalidActionPayload(Exception):
    """The payload of the request does not fit the model of its action."""

    def __init__(self, action_name: str, fields: tuple[str, ...]) -> None:
        super().__init__(
            f"payload of action {shown(action_name)} does not fit{fields_phrase(fields)}"
        )
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
class ActionPolicy:
    """Limits of one action. An action is tried again only when it fails in a way that may pass.

    ``max_attempts`` counts the first one and is a whole number from 1 to ``MAX_ATTEMPTS``:
    Temporal reads 0 as no limit, and an action has a side effect. ``attempt_timeout_seconds``
    and ``retry_after_seconds`` are finite numbers above 0, at most ``MAX_ATTEMPT_TIMEOUT_SECONDS``
    and ``MAX_RETRY_AFTER_SECONDS``. ``heartbeat_timeout_seconds`` is for an action that runs
    long: a worker that stops heartbeating is noticed within it, and the workflow's
    cancellation reaches the action at a heartbeat. It is no longer than the attempt it
    watches. The activity heartbeats three times in it, taking the timeout from what the
    workflow scheduled it with, so this is the one place it is set. A short action needs none.

    The values are checked when the policy is made, so a policy is made when the
    agent's module is loaded (``Agent.action``) and is never part of a workflow's
    input: a ``ValueError`` while Temporal decodes the input fails the workflow
    task, which is tried again for ever, and so would a duration of ``nan`` seconds.
    """

    attempt_timeout_seconds: float
    max_attempts: int
    retry_after_seconds: float
    heartbeat_timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        require_positive(
            attempt_timeout_seconds=self.attempt_timeout_seconds,
            max_attempts=self.max_attempts,
            retry_after_seconds=self.retry_after_seconds,
        )
        if not isinstance(self.max_attempts, int):
            raise ValueError(f"max_attempts must be a whole number, not {self.max_attempts}")
        _require_at_most(
            attempt_timeout_seconds=(self.attempt_timeout_seconds, MAX_ATTEMPT_TIMEOUT_SECONDS),
            max_attempts=(self.max_attempts, MAX_ATTEMPTS),
            retry_after_seconds=(self.retry_after_seconds, MAX_RETRY_AFTER_SECONDS),
        )
        if self.heartbeat_timeout_seconds is not None:
            require_positive(heartbeat_timeout_seconds=self.heartbeat_timeout_seconds)
            _require_at_most(
                heartbeat_timeout_seconds=(
                    self.heartbeat_timeout_seconds,
                    self.attempt_timeout_seconds,
                )
            )


@dataclass(frozen=True)
class ActionDefinition:
    """An allowed action: its name and the model its payload must fit."""

    name: str
    payload: type[BaseModel]

    def __post_init__(self) -> None:
        # Without this a payload with a misspelled or invented field would pass unnoticed, and
        # the library's default for a nested model is to drop what it does not know.
        try:
            lenient = next(lenient_types(self.payload), None)
        except UnreadableModel as e:
            raise InvalidActionDefinition(f"payload of action {self.name!r}: {e}") from None
        if lenient is not None:
            raise InvalidActionDefinition(
                f"{lenient.__name__} in the payload of action {self.name!r} "
                "must forbid extra fields (extra='forbid' in its config)"
            )
        # A payload is text a model wrote: a path or an import name in it would let the model
        # choose what the worker opens or imports.
        named = next(fields_that_name_files_or_code(self.payload), None)
        if named is not None:
            structure, field = named
            raise InvalidActionDefinition(
                f"field {field!r} of {structure.__name__} in the payload of action {self.name!r} "
                "reads text a model wrote as a file path or an import name: "
                "take it as a string and let a handler decide what it may name"
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
            return read_strict_json(definition.payload, request.payload)
        except DoesNotFit as e:
            raise InvalidActionPayload(request.name, e.fields) from None


def mark_as_action(model: type[BaseModel], name: str) -> None:
    """Make ``model`` the payload of the action ``name``, so that ``action_name_of`` finds it.

    A model carries one name: it is marked once, and a second mark is refused
    instead of silently changing what the first agent's nodes ask for. Only the
    class that was marked carries the name, not a subclass of it.
    """
    taken = vars(model).get(_ACTION_NAME)
    if taken is not None:
        raise InvalidActionDefinition(
            f"model {model.__name__} is already the payload of action {taken!r}"
        )
    setattr(model, _ACTION_NAME, name)


def action_name_of(payload: BaseModel) -> str:
    """The name of the action that ``payload``'s model was registered as.

    Raises ``NotAnAction`` for a model that was not.
    """
    name: str | None = vars(type(payload)).get(_ACTION_NAME)
    if name is None:
        raise NotAnAction(type(payload))
    return name


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


def require_positive(**limits: float) -> None:
    """Raises ``ValueError`` naming each limit that is not a finite number above 0.

    A bool is not a number here, and ``nan`` and infinity are not finite: a comparison with 0
    lets them through, and a duration made of one raises where it is used, which in a
    workflow fails the workflow task again and again.
    """
    for name, value in limits.items():
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{name} must be a finite number more than 0, not {value!r}")
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a finite number more than 0, not {value}")


def _require_at_most(**limits: tuple[float, float]) -> None:
    for name, (value, ceiling) in limits.items():
        if value > ceiling:
            raise ValueError(f"{name} must be at most {ceiling}, not {value}")
