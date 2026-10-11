"""Tests for what any agent's workflow uses: words for a failure, and a database write.

``reason_shown`` is a plain function and is tested as one, with the failures Temporal raises to a
workflow made by hand. ``database_write`` runs in the workflow sandbox against the dev stack's
Temporal server (see ``conftest.py`` of the worker's tests), with a scripted activity.
"""

from dataclasses import dataclass, field

import pytest
from scripted_agent import REFUSED, Write, WritesToTheDatabase
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    CancelledError,
    RetryState,
    TimeoutError,
    TimeoutType,
)
from temporalio.worker import Worker

from openmarketer_worker.agent_runtime.vocabulary import DatabaseWritePolicy, FailureTexts
from openmarketer_worker.agent_runtime.workflow_support import reason_shown

# An agent's own words: the helper holds none.
TEXTS = FailureTexts(
    stopped_unexpectedly="the campaign stopped unexpectedly",
    timed_out="the campaign did not finish in time",
    worker_stopped="the worker running the campaign stopped responding",
    cancelled="the campaign was cancelled",
    database_failed="the database could not be reached",
)
WORDED = ("CampaignRefused", "ChannelUnavailable")


def from_an_activity(cause: BaseException | None) -> ActivityError:
    """The failure a workflow gets when an activity ends with ``cause``."""
    error = ActivityError(
        "Activity task failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="worker",
        activity_type="send_campaign",
        activity_id="1",
        retry_state=RetryState.MAXIMUM_ATTEMPTS_REACHED,
    )
    error.__cause__ = cause
    return error


# ------------------------------------------------------------------ reason_shown
def test_cancelled_workflow_is_shown_as_cancelled():
    assert reason_shown(CancelledError("cancelled"), TEXTS) == TEXTS.cancelled


def test_activity_that_was_cancelled_is_shown_as_cancelled():
    assert reason_shown(from_an_activity(CancelledError("cancelled")), TEXTS) == TEXTS.cancelled


def test_failure_an_activity_worded_is_shown_with_its_own_message():
    refused = ApplicationError("no audience after 40 tries", type="CampaignRefused")
    assert reason_shown(from_an_activity(refused), TEXTS, worded_types=WORDED) == refused.message


def test_failure_of_a_type_nobody_worded_is_shown_in_the_fixed_words():
    leaking = ApplicationError("/tmp/clone-1234 not found", type="FileNotFoundError")
    assert (
        reason_shown(from_an_activity(leaking), TEXTS, worded_types=WORDED)
        == TEXTS.stopped_unexpectedly
    )


def test_every_failure_is_worded_when_the_agent_gives_no_types():
    worded_by_a_handler = ApplicationError("the mail server refused it", type="Refused")
    assert (
        reason_shown(from_an_activity(worded_by_a_handler), TEXTS) == "the mail server refused it"
    )


def test_message_that_says_nothing_is_shown_in_the_fixed_words():
    silent = ApplicationError("", type="Refused")
    assert reason_shown(from_an_activity(silent), TEXTS) == TEXTS.stopped_unexpectedly


def test_failure_the_workflow_raised_itself_is_shown_with_its_own_message():
    too_many = ApplicationError("the run asked for more than 2 actions", type="TooManyActions")
    assert reason_shown(too_many, TEXTS, worded_types=("TooManyActions",)) == too_many.message


def test_attempt_that_took_too_long_is_shown_as_timed_out():
    slow = TimeoutError("timed out", type=TimeoutType.START_TO_CLOSE, last_heartbeat_details=[])
    assert reason_shown(from_an_activity(slow), TEXTS) == TEXTS.timed_out


def test_worker_that_stopped_sending_heartbeats_is_shown_as_stopped():
    silent = TimeoutError("timed out", type=TimeoutType.HEARTBEAT, last_heartbeat_details=[])
    assert reason_shown(from_an_activity(silent), TEXTS) == TEXTS.worker_stopped


def test_failure_without_a_cause_is_shown_in_the_fixed_words():
    assert reason_shown(from_an_activity(None), TEXTS) == TEXTS.stopped_unexpectedly
    assert (
        reason_shown(RuntimeError("a message written for a log"), TEXTS)
        == TEXTS.stopped_unexpectedly
    )


# ------------------------------------------------------------------ database_write
@dataclass
class ScriptedRowWriter:
    """An activity that takes the next of ``outcomes`` for each call: an error, or success."""

    outcomes: list[Exception | None] = field(default_factory=list)
    calls: int = 0

    @activity.defn(name="store_row")
    async def store_row(self, row: str) -> str:
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome
        return f"stored {row}"


QUICK_WRITE = DatabaseWritePolicy(
    attempt_timeout_seconds=10, retry_after_seconds=0.1, max_retry_wait_seconds=0.5, max_attempts=3
)


async def write(temporal, task_queue, writer: ScriptedRowWriter, limits: DatabaseWritePolicy):
    """Run the workflow that writes once, on a worker with ``writer``; its result."""
    worker = Worker(
        temporal,
        task_queue=task_queue,
        workflows=[WritesToTheDatabase],
        activities=[writer.store_row],
    )
    async with worker:
        return await temporal.execute_workflow(
            WritesToTheDatabase.run,
            Write("store_row", limits),
            id=f"write-{task_queue}",
            task_queue=task_queue,
        )


async def test_write_that_works_returns_what_the_activity_returned(temporal, task_queue):
    writer = ScriptedRowWriter()
    assert await write(temporal, task_queue, writer, QUICK_WRITE) == "stored the row"
    assert writer.calls == 1


async def test_write_that_fails_is_tried_again(temporal, task_queue):
    writer = ScriptedRowWriter([ApplicationError("the database is busy", type="Busy"), None])
    assert await write(temporal, task_queue, writer, QUICK_WRITE) == "stored the row"
    assert writer.calls == 2


async def test_write_that_keeps_failing_ends_after_the_attempts_of_its_policy(temporal, task_queue):
    busy = ApplicationError("the database is busy", type="Busy")
    writer = ScriptedRowWriter([busy] * 5)
    with pytest.raises(WorkflowFailureError) as failed:
        await write(temporal, task_queue, writer, QUICK_WRITE)
    assert isinstance(failed.value.__cause__, ActivityError)
    assert writer.calls == QUICK_WRITE.max_attempts


async def test_write_that_fails_with_a_type_the_agent_does_not_retry_is_not_tried_again(
    temporal, task_queue
):
    writer = ScriptedRowWriter([ApplicationError("the run is over", type=REFUSED), None])
    with pytest.raises(WorkflowFailureError) as failed:
        await write(temporal, task_queue, writer, QUICK_WRITE)
    cause = failed.value.__cause__
    assert isinstance(cause, ActivityError)
    assert cause.retry_state is RetryState.NON_RETRYABLE_FAILURE
    assert writer.calls == 1


async def test_write_that_may_be_tried_for_a_while_gives_up_when_the_time_is_over(
    temporal, task_queue
):
    patient = DatabaseWritePolicy(
        attempt_timeout_seconds=10,
        retry_after_seconds=0.1,
        max_retry_wait_seconds=0.2,
        give_up_after_seconds=4,
    )
    busy = ApplicationError("the database is busy", type="Busy")
    writer = ScriptedRowWriter([busy] * 1000)
    with pytest.raises(WorkflowFailureError) as failed:
        await write(temporal, task_queue, writer, patient)
    gave_up = failed.value.__cause__
    assert isinstance(gave_up, ActivityError)
    assert gave_up.retry_state is RetryState.TIMEOUT
    # It was tried again, there being no limit of attempts, until the time was over.
    assert writer.calls >= 2
