"""The actions the agent runtime's tests run: models, handlers that do what a test tells them.

A handler remembers what it was asked (the validated payload and the context) and takes the
next of the outcomes a test gave it: an error to raise or a result to return. No service is
involved.
"""

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict
from temporalio import activity
from temporalio.exceptions import ApplicationError

from openmarketer_core.agent_runtime.actions import ActionDefinition
from openmarketer_worker.agent_runtime.routes import (
    ActionContext,
    ActionHandler,
    ActionRoute,
    ActionRoutes,
)


class SendEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: str


class Publish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    post: int


@dataclass
class ScriptedAction:
    """A handler that takes the next of ``outcomes`` for each call: an error to raise, or a result.

    With no outcome left it succeeds with an empty result.
    """

    outcomes: list[Exception | Mapping[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.calls: list[tuple[BaseModel, ActionContext]] = []

    def __call__(self, payload: BaseModel, context: ActionContext) -> Mapping[str, Any]:
        self.calls.append((payload, context))
        outcome = self.outcomes.pop(0) if self.outcomes else {}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @property
    def action_ids(self) -> list[str]:
        return [context.action_id for _, context in self.calls]


class BlockingAction:
    """A handler that does not return until it is released: an action that is still running."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, payload: BaseModel, context: ActionContext) -> Mapping[str, Any]:
        self.started.set()
        self.release.wait(timeout=60)
        return {}


class SlowAction:
    """A handler that works for ``seconds`` and then succeeds: an action longer than a heartbeat."""

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds

    def __call__(self, payload: BaseModel, context: ActionContext) -> Mapping[str, Any]:
        time.sleep(self._seconds)
        return {"message_id": "m-1"}


class WatchingAction:
    """A handler that works until the activity it runs in is cancelled, and says why it was."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.saw_cancellation = threading.Event()
        self.why: activity.ActivityCancellationDetails | None = None

    def __call__(self, payload: BaseModel, context: ActionContext) -> Mapping[str, Any]:
        self.started.set()
        for _ in range(600):
            if activity.is_cancelled():
                self.why = activity.cancellation_details()
                self.saw_cancellation.set()
                break
            time.sleep(0.05)
        return {}


def worded_while_handling(secret_text: str) -> ApplicationError:
    """An error worded inside an ``except`` block, so Python links it to the error it handled.

    The error handled has ``secret_text`` in its message, as a library's error holds the URL of
    the request that failed.
    """
    try:
        try:
            raise ConnectionError(f"GET https://api.example.com/send?key={secret_text} failed")
        except ConnectionError:
            # The link to the error handled is what this is about.
            raise ApplicationError(  # noqa: B904
                "the service refused the message", type="Refused", non_retryable=True
            )
    except ApplicationError as worded:
        return worded


def nested(depth: int) -> dict[str, Any]:
    """A result that is JSON, ``depth`` levels deep."""
    value: dict[str, Any] = {}
    for _ in range(depth):
        value = {"x": value}
    return value


def route(name: str, model: type[BaseModel], handler: ActionHandler) -> ActionRoute:
    return ActionRoute(ActionDefinition(name, model), handler)


def routes_of(send: ActionHandler, publish: ActionHandler) -> ActionRoutes:
    return ActionRoutes([route("send_email", SendEmail, send), route("publish", Publish, publish)])
