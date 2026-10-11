"""Reading what a workflow's history holds, as the tests need it and no more.

Payloads in history are stored encoded; the tests ask two things of it: whether a text is in it
anywhere (``readable``), and what each activity was given and returned (``exchanges``). Both read
the history as the server stores it, so no secret can hide in a field this module does not decode.
"""

import json
from dataclasses import dataclass
from typing import Any

from temporalio.client import WorkflowHistory


def readable(history: WorkflowHistory) -> str:
    """Everything the history holds, as text: payloads, failure messages and all.

    Payloads of the default data converter are JSON text, so a value shows up in the bytes of
    the events as it was written.
    """
    return b"\n".join(event.SerializeToString() for event in history.events).decode(
        errors="replace"
    )


def failures_in(history: WorkflowHistory) -> str:
    """What the history holds of the failures of activities and of the workflow, as text."""
    return b"\n".join(
        event.SerializeToString()
        for event in history.events
        if event.HasField("activity_task_failed_event_attributes")
        or event.HasField("workflow_execution_failed_event_attributes")
    ).decode(errors="replace")


@dataclass(frozen=True)
class Exchange:
    """What one scheduled activity was given and what it returned, decoded.

    ``returned`` is None for an activity that did not complete.
    """

    activity: str
    given: Any
    returned: Any


def exchanges(history: WorkflowHistory) -> list[Exchange]:
    """Every activity that was scheduled, in the order it was, with its input and its result."""
    scheduled: dict[int, tuple[str, Any]] = {}
    results: dict[int, Any] = {}
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            attributes = event.activity_task_scheduled_event_attributes
            (given,) = attributes.input.payloads
            scheduled[event.event_id] = (attributes.activity_type.name, json.loads(given.data))
        elif event.HasField("activity_task_completed_event_attributes"):
            attributes = event.activity_task_completed_event_attributes
            (returned,) = attributes.result.payloads
            results[attributes.scheduled_event_id] = json.loads(returned.data)
    return [
        Exchange(activity, given, results.get(event_id))
        for event_id, (activity, given) in scheduled.items()
    ]
