"""Reading what a workflow's history holds, as the tests need it and no more.

Payloads in history are stored encoded; the tests ask two things of it: whether a text is in it
anywhere (``readable``), and what each activity was given and returned (``exchanges``). Both read
the history as the server stores it, so no secret can hide in a field this module does not decode.
``scheduled_with`` reads the limits an activity was scheduled with.
"""

import json
from dataclasses import dataclass
from datetime import timedelta
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

    ``returned`` is None for an activity that did not complete, or returned nothing.
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
            # An activity that returns nothing is stored as a payload with no data.
            results[attributes.scheduled_event_id] = (
                json.loads(returned.data) if returned.data else None
            )
    return [
        Exchange(activity, given, results.get(event_id))
        for event_id, (activity, given) in scheduled.items()
    ]


def completed(history: WorkflowHistory) -> list[str]:
    """The names of the activities that completed, in the order they did.

    An activity that was scheduled and never completed is not in it: a workflow that closed
    while its activity was pending leaves no completion behind.
    """
    scheduled: dict[int, str] = {}
    names: list[str] = []
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            scheduled[event.event_id] = (
                event.activity_task_scheduled_event_attributes.activity_type.name
            )
        elif event.HasField("activity_task_completed_event_attributes"):
            names.append(
                scheduled[event.activity_task_completed_event_attributes.scheduled_event_id]
            )
    return names


@dataclass(frozen=True)
class ScheduledWith:
    """The limits Temporal was asked to run an activity under."""

    start_to_close: timedelta
    heartbeat: timedelta | None
    max_attempts: int
    first_wait: timedelta


def scheduled_with(history: WorkflowHistory, activity: str) -> ScheduledWith:
    """The limits the first scheduling of ``activity`` in the history asked for."""
    for event in history.events:
        attributes = event.activity_task_scheduled_event_attributes
        if event.HasField("activity_task_scheduled_event_attributes") and (
            attributes.activity_type.name == activity
        ):
            return ScheduledWith(
                start_to_close=attributes.start_to_close_timeout.ToTimedelta(),
                heartbeat=(
                    attributes.heartbeat_timeout.ToTimedelta()
                    if attributes.HasField("heartbeat_timeout")
                    else None
                ),
                max_attempts=attributes.retry_policy.maximum_attempts,
                first_wait=attributes.retry_policy.initial_interval.ToTimedelta(),
            )
    raise AssertionError(f"the history schedules no activity {activity}")
