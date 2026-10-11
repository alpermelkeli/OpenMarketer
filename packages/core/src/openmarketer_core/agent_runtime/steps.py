"""What taking one step of an agent graph gives back, how a run is named, and why a step is refused.

A step is one stretch of a graph on its thread: from where it stands to where
it finishes or has to wait for the outside world. ``StepOutcome`` says which
of the two it was, as plain data that the workflow engine's default converter
can carry between activities. ``GraphRunRef`` names the run a step belongs to
by ids only, so no repository, profile or token is ever in the history of the
workflow.

The errors say why a thread cannot take the step it was asked to. They are
here, and not with the graph framework that raises them, so that the worker can
tell them apart from a failure of the database or of a model without loading
the framework.

This module takes no step: how one is taken is in ``langgraph_steps.py``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from openmarketer_core.agent_runtime.actions import ActionRequest


@dataclass(frozen=True)
class GraphRunRef:
    """A run of a graph: the kind of run (see ``threads.thread_id``) and its id."""

    kind: str
    run_id: uuid.UUID


@dataclass(frozen=True)
class StepOutcome:
    """The end of a step: the graph finished with ``output``, or waits for ``action``.

    Exactly one of the two is set. ``output`` is the graph's final state as
    plain data.
    """

    output: dict[str, Any] | None
    action: ActionRequest | None

    def __post_init__(self) -> None:
        if (self.output is None) == (self.action is None):
            raise ValueError("a step either finished with an output or waits for an action")

    @classmethod
    def finished(cls, output: dict[str, Any]) -> StepOutcome:
        return cls(output=output, action=None)

    @classmethod
    def waiting_for(cls, action: ActionRequest) -> StepOutcome:
        return cls(output=None, action=action)


class StepError(Exception):
    """A thread cannot take the step it was asked to; asking again will not change that."""


class MissingInitialState(StepError):
    """The thread has no checkpoint, so only an initial state can start it, and none was given."""

    def __init__(self, thread_id: str) -> None:
        super().__init__(f"thread {thread_id} has no checkpoint and was given no initial state")


class UnexpectedResume(StepError):
    """The outcome answers an action that the thread never asked for."""

    def __init__(self, thread_id: str, action_id: str) -> None:
        super().__init__(f"outcome of action {action_id} answers no action of thread {thread_id}")


class TooManyPendingActions(StepError):
    """The thread waits for several actions at once; a graph asks for one at a time."""

    def __init__(self, thread_id: str, count: int) -> None:
        super().__init__(
            f"thread {thread_id} waits for {count} actions at once: "
            "a graph may ask for one action at a time"
        )


class MalformedActionRequest(StepError):
    """The thread paused on something that is not an action request."""

    def __init__(self, thread_id: str) -> None:
        super().__init__(
            f"thread {thread_id} paused on something that is not an action request: "
            "nodes ask for an action with request_action"
        )
