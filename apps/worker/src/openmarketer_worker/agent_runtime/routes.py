"""The actions an agent can run: for each, its name and payload model, and what runs it.

A graph asks for an action by name, with a payload the model may have shaped.
``ActionRoutes`` is what the worker holds to answer: the ``ActionCatalog`` that
decides whether the name and the payload are allowed (core's rule) and the
handler that runs the action. One route holds both so that an action cannot be
allowed without a handler. An action that starts another agent's run runs no
handler: its route holds the ``RunTarget`` of the run it starts and the
``PrepareRun`` callable that creates the child run's record (``RunRoutes``).

The limits an action runs under (timeouts, attempts, heartbeats) are not here but in the
workflow, which is the one place they are set: its ``actions`` mapping names the actions a
run may ask for, with a core ``ActionPolicy`` each (``Agent.policies`` is one, made by the
agent's ``action`` decorators). A name in one and not in the other is refused
where it is found: the workflow refuses a name it does not list before anything runs, and
the activity refuses one its routes do not hold.

Built when the worker starts, by the wiring of the agent whose actions these
are: a handler is handed what it needs (a session factory, a client of a
service) like any activity. It holds no action of its own and runs none:
``activities.py`` validates a request with the catalog and calls the handler.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from openmarketer_core.agent_runtime.actions import (
    ActionCatalog,
    ActionDefinition,
    RunTarget,
)
from openmarketer_core.agent_runtime.gate import RunScope


@dataclass(frozen=True)
class ActionContext:
    """What a handler gets besides its payload.

    ``action_id`` is the idempotency key of the work. Activities run at least
    once, so a handler that has a side effect uses it to tell a repeat of
    work it did from new work, for instance as the key of a request to the
    service it calls.
    """

    action_id: str


# Runs the action with its validated payload and answers with small JSON. It is blocking and
# runs in a thread, where ``temporalio.activity.is_cancelled()`` says whether the workflow gave
# up on it. What it raises that is not an ``ApplicationError`` it worded is logged and replaced
# by a failure that is tried again. One it worded leaves with its type and message and nothing
# it was raised from, and its message is what the graph is told of the failure (cut to
# ``MAX_ERROR_CHARACTERS``): it holds nothing the model must not see.
ActionHandler = Callable[[BaseModel, ActionContext], Mapping[str, Any]]


@dataclass(frozen=True)
class ActionRoute:
    """One allowed action: what it is called and takes, and what runs it."""

    definition: ActionDefinition
    handler: ActionHandler


class ActionRoutes:
    """The routes of an agent's actions, by action name."""

    def __init__(self, routes: Iterable[ActionRoute]) -> None:
        """Raises ``InvalidActionDefinition`` when two routes have the same name."""
        held = tuple(routes)
        self.catalog = ActionCatalog(route.definition for route in held)
        self._by_name = {route.definition.name: route for route in held}

    def route(self, name: str) -> ActionRoute:
        """The route of the action ``name``, which a request that passed the catalog names."""
        return self._by_name[name]


@dataclass(frozen=True)
class RunContext:
    """What a run action's prepare gets besides its payload.

    ``action_id`` is the idempotency key of the action, and ``scope`` the workspace and
    project of the run that asked: the child run's record belongs to them, and the callable
    reaches the database with them.
    """

    action_id: str
    scope: RunScope


# Creates the child run's record, keyed by ``RunContext.action_id`` so that an attempt that is
# tried again finds the same run, and answers with its id. It is blocking and runs in a thread,
# like a handler. What it raises that is not worded is logged and reported as unexpected.
type PrepareRun[Payload: BaseModel] = Callable[[Payload, RunContext], uuid.UUID]


@dataclass(frozen=True)
class RunRoute:
    """One action that starts another agent's run: what it takes, and how its run is created."""

    definition: ActionDefinition
    target: RunTarget
    prepare: PrepareRun


class RunRoutes:
    """The run actions of an agent, by action name."""

    def __init__(self, routes: Iterable[RunRoute]) -> None:
        """Raises ``InvalidActionDefinition`` when two routes have the same name."""
        held = tuple(routes)
        self.catalog = ActionCatalog(route.definition for route in held)
        self._by_name = {route.definition.name: route for route in held}

    def route(self, name: str) -> RunRoute:
        """The route of the run action ``name``, which a request that passed the catalog names."""
        return self._by_name[name]
