"""Tests for the helpers that read a workflow history, on a history made by hand.

The tests of what reaches the history rest on ``readable``, ``failures_in`` and ``exchanges``,
so they are shown to find a value that is there and to pair what an activity was given with what
it returned, on events shaped as the server stores them.
"""

from scripted_history import exchanges, failures_in, readable
from temporalio.api.common.v1 import Payload
from temporalio.api.failure.v1 import Failure
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import WorkflowHistory
from temporalio.converter import DataConverter


def payload_of(value: object) -> Payload:
    (payload,) = DataConverter.default.payload_converter.to_payloads([value])
    return payload


def scheduled(event_id: int, activity: str, given: object) -> HistoryEvent:
    event = HistoryEvent(event_id=event_id)
    attributes = event.activity_task_scheduled_event_attributes
    attributes.activity_type.name = activity
    attributes.input.payloads.append(payload_of(given))
    return event


def completed(event_id: int, scheduled_event_id: int, returned: object) -> HistoryEvent:
    event = HistoryEvent(event_id=event_id)
    event.activity_task_completed_event_attributes.scheduled_event_id = scheduled_event_id
    event.activity_task_completed_event_attributes.result.payloads.append(payload_of(returned))
    return event


def failed(event_id: int, message: str) -> HistoryEvent:
    event = HistoryEvent(event_id=event_id)
    event.activity_task_failed_event_attributes.failure.CopyFrom(Failure(message=message))
    return event


def history_of(*events: HistoryEvent) -> WorkflowHistory:
    return WorkflowHistory("a-workflow", list(events))


def test_value_in_a_payload_of_the_history_is_found():
    history = history_of(scheduled(1, "send", {"to": "ada@example.com"}))
    assert "ada@example.com" in readable(history)
    assert "grace@example.com" not in readable(history)


def test_value_in_a_failure_of_the_history_is_found():
    history = history_of(failed(1, "the service refused the message"))
    assert "the service refused the message" in readable(history)


def test_failures_are_those_of_activities_and_not_what_they_were_given():
    history = history_of(
        scheduled(1, "send", {"to": "ada@example.com"}), failed(2, "the service refused")
    )
    assert "the service refused" in failures_in(history)
    assert "ada@example.com" not in failures_in(history)


def test_each_activity_is_paired_with_the_result_of_its_own_completion():
    history = history_of(
        scheduled(1, "send", {"to": "ada"}),
        scheduled(2, "publish", {"post": 7}),
        completed(3, 2, {"url": "u"}),
        completed(4, 1, {"message_id": "m-1"}),
    )
    assert [(e.activity, e.given, e.returned) for e in exchanges(history)] == [
        ("send", {"to": "ada"}, {"message_id": "m-1"}),
        ("publish", {"post": 7}, {"url": "u"}),
    ]


def test_activity_that_did_not_complete_has_no_result():
    history = history_of(scheduled(1, "send", {"to": "ada"}), failed(2, "no"))
    assert [(e.activity, e.returned) for e in exchanges(history)] == [("send", None)]
