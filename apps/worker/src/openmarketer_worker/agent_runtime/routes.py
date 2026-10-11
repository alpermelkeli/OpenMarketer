"""The actions an agent can run: for each, its name and payload model, what runs it and its limits.

A graph asks for an action by name, with a payload the model may have shaped.
``ActionRoutes`` is what the worker holds to answer: the ``ActionCatalog`` that
decides whether the name and the payload are allowed (core's rule), the
handler that runs the action, and the policy it runs under. One route holds
all three so that an action cannot be allowed without a handler or run without
limits.

Built when the worker starts, by the wiring of the agent whose actions these
are: a handler is handed what it needs (a session factory, a client of a
service) like any activity. It holds no action of its own and runs none:
``activities.py`` validates a request with the catalog and calls the handler.
The workflow does not load this module; it is given ``policies``, which is
plain data.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from openmarketer_core.agent_runtime.actions import ActionCatalog, ActionDefinition
from openmarketer_worker.agent_runtime.vocabulary import ActionPolicy


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
# runs in a thread. What it raises that is not an ``ApplicationError`` it worded is logged and
# replaced by a failure that is tried again; one it worded passes as it is.
ActionHandler = Callable[[BaseModel, ActionContext], Mapping[str, Any]]


@dataclass(frozen=True)
class ActionRoute:
    """One allowed action: what it is called and takes, what runs it, and under which limits."""

    definition: ActionDefinition
    handler: ActionHandler
    policy: ActionPolicy


class ActionRoutes:
    """The routes of an agent's actions, by action name."""

    def __init__(self, routes: Iterable[ActionRoute]) -> None:
        """Raises ``InvalidActionDefinition`` when two routes have the same name."""
        held = tuple(routes)
        self.catalog = ActionCatalog(route.definition for route in held)
        self._by_name = {route.definition.name: route for route in held}
        # The plain mapping an agent's workflow gives the driver: names with their limits.
        self.policies: Mapping[str, ActionPolicy] = {
            name: route.policy for name, route in self._by_name.items()
        }

    def route(self, name: str) -> ActionRoute:
        """The route of the action ``name``, which a request that passed the catalog names."""
        return self._by_name[name]
