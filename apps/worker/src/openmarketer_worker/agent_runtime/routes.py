"""The actions an agent can run: for each, its name and payload model, and what runs it.

A graph asks for an action by name, with a payload the model may have shaped.
``ActionRoutes`` is what the worker holds to answer: the ``ActionCatalog`` that
decides whether the name and the payload are allowed (core's rule) and the
handler that runs the action. One route holds both so that an action cannot be
allowed without a handler.

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

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from openmarketer_core.agent_runtime.actions import ActionCatalog, ActionDefinition


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
