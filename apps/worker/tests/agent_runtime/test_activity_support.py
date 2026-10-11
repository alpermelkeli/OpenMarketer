"""Tests for what activities of any agent owe the engine: worded failures, heartbeats, checkpoints.

Heartbeats and cancellation run in Temporal's activity environment, which needs no server;
the checkpoints are the real ones in PostgreSQL, except where the database is made unreachable.
"""

import asyncio
import logging
import threading
from dataclasses import replace
from datetime import timedelta

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from scripted_actions import nested, worded_while_handling
from scripted_rig import happened
from sqlalchemy import text
from temporalio.api.failure.v1 import Failure
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError, ApplicationErrorCategory
from temporalio.testing import ActivityEnvironment

from openmarketer_core.agent_runtime.actions import ActionOutcome
from openmarketer_core.db.session import DatabaseError
from openmarketer_core.graph_checkpoints.postgres import AttemptInProgress
from openmarketer_worker.agent_runtime.activity_support import (
    CheckpointAccess,
    NotStorable,
    checkpoints_in,
    forget_quietly,
    run_in_thread,
    run_with_heartbeats,
    stored_size,
    worded_failures,
)
from openmarketer_worker.agent_runtime.vocabulary import (
    DATABASE_UNAVAILABLE,
    UNEXPECTED,
    FailureTexts,
)

# An agent's own words: the helpers hold none.
CAMPAIGN_TEXTS = FailureTexts(
    stopped_unexpectedly="the campaign stopped unexpectedly",
    timed_out="the campaign did not finish in time",
    worker_stopped="the worker running the campaign stopped responding",
    cancelled="the campaign was cancelled",
    database_failed="the database could not be reached",
)
SUBJECT = "campaign run 7"
PASSWORD = "hunter2"


# ------------------------------------------------------------------ worded_failures
def leaving_an_activity(error: BaseException) -> ApplicationError:
    """What leaves an activity that raised ``error`` inside ``worded_failures``."""
    with pytest.raises(ApplicationError) as raised:
        with worded_failures(CAMPAIGN_TEXTS, subject=SUBJECT):
            raise error
    return raised.value


def as_stored(error: BaseException) -> Failure:
    """The failure Temporal writes to the history for ``error``, with every cause it follows."""
    converter = DataConverter.default
    stored = Failure()
    converter.failure_converter.to_failure(error, converter.payload_converter, stored)
    return stored


def test_worded_failure_leaves_without_the_error_it_was_worded_while_handling():
    error = leaving_an_activity(worded_while_handling(PASSWORD))
    stored = as_stored(error)
    assert not stored.HasField("cause")
    assert PASSWORD not in str(stored)
    assert stored.message == "the service refused the message"


def test_worded_failure_leaves_with_everything_it_was_worded_with():
    worded = ApplicationError(
        "wait a moment",
        {"after": 5},
        type="RateLimited",
        non_retryable=True,
        next_retry_delay=timedelta(seconds=7),
        category=ApplicationErrorCategory.BENIGN,
    )
    error = leaving_an_activity(worded)
    assert (error.message, error.details, error.type, error.non_retryable) == (
        "wait a moment",
        ({"after": 5},),
        "RateLimited",
        True,
    )
    assert (error.next_retry_delay, error.category) == (
        timedelta(seconds=7),
        ApplicationErrorCategory.BENIGN,
    )


def test_worded_failure_without_a_type_leaves_without_one():
    assert leaving_an_activity(ApplicationError("no type")).type is None


def test_failure_an_activity_worded_leaves_with_its_type_and_message():
    worded = ApplicationError("no audience", type="CampaignRefused", non_retryable=True)
    error = leaving_an_activity(worded)
    assert (error.type, error.message, error.non_retryable) == (
        "CampaignRefused",
        "no audience",
        True,
    )


def test_failure_of_the_database_becomes_one_worth_another_attempt_in_the_agents_words(caplog):
    error = leaving_an_activity(DatabaseError(f"connection to host with password {PASSWORD} lost"))
    assert (error.type, error.message, error.non_retryable) == (
        DATABASE_UNAVAILABLE,
        "the database could not be reached",
        False,
    )
    assert error.__cause__ is None
    assert "campaign run 7: the database could not be used" in caplog.text
    assert PASSWORD in caplog.text  # the detail is for the log, not for the run


def test_error_nobody_worded_becomes_an_unexpected_failure_without_its_detail(caplog):
    error = leaving_an_activity(ValueError(f"/Users/someone/clone-1234 {PASSWORD}"))
    assert (error.type, error.message, error.non_retryable) == (
        UNEXPECTED,
        "the campaign stopped unexpectedly",
        False,
    )
    assert error.__cause__ is None
    assert PASSWORD not in str(error)
    assert "campaign run 7 stopped on an unexpected error" in caplog.text
    assert "/Users/someone/clone-1234" in caplog.text


def test_cancellation_is_not_an_error_to_word():
    with pytest.raises(asyncio.CancelledError), worded_failures(CAMPAIGN_TEXTS, subject=SUBJECT):
        raise asyncio.CancelledError


def test_block_that_works_is_left_alone():
    with worded_failures(CAMPAIGN_TEXTS, subject=SUBJECT):
        worked = True
    assert worked


# ------------------------------------------------------------------ run_with_heartbeats
def in_an_activity(environment: ActivityEnvironment, work, *, every_seconds: float = 0.02):
    """``run_with_heartbeats`` of ``work`` as an activity of ``environment`` runs it."""

    async def activity_body():
        return await run_with_heartbeats(work, every_seconds=every_seconds, subject=SUBJECT)

    return environment.run(activity_body)


async def test_work_runs_in_a_thread_and_its_result_is_returned():
    def work() -> str:
        return threading.current_thread().name

    ran_in = await in_an_activity(ActivityEnvironment(), work)
    assert ran_in != threading.current_thread().name


async def test_heartbeats_are_sent_while_the_work_runs():
    environment = ActivityEnvironment()
    heartbeats: list[object] = []
    environment.on_heartbeat = lambda *details: heartbeats.append(details)
    release = threading.Event()
    running = asyncio.create_task(in_an_activity(environment, release.wait))
    await asyncio.sleep(0.3)
    release.set()
    await running
    assert len(heartbeats) >= 3


async def test_error_the_work_raised_is_raised_to_the_activity():
    def work() -> None:
        raise RuntimeError("the clone failed")

    with pytest.raises(RuntimeError, match="the clone failed"):
        await in_an_activity(ActivityEnvironment(), work)


async def test_cancelled_activity_says_so_in_the_log_and_leaves_the_thread_to_finish(caplog):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def work() -> None:
        started.set()
        release.wait(timeout=30)
        finished.set()

    environment = ActivityEnvironment()
    running = asyncio.create_task(in_an_activity(environment, work))
    try:
        await happened(started)
        with caplog.at_level(logging.WARNING):
            environment.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        assert not finished.is_set()
    finally:
        release.set()
    await happened(finished)
    assert "campaign run 7 was cancelled while its work was running" in caplog.text


# ------------------------------------------------------------------ run_in_thread
def with_a_heartbeat_timeout(environment: ActivityEnvironment, seconds: float) -> None:
    environment.info = replace(environment.info, heartbeat_timeout=timedelta(seconds=seconds))


async def test_work_is_heartbeated_at_a_third_of_the_timeout_the_workflow_scheduled_it_with():
    environment, heartbeats = ActivityEnvironment(), []
    with_a_heartbeat_timeout(environment, 0.06)
    environment.on_heartbeat = lambda *details: heartbeats.append(details)
    release = threading.Event()

    async def in_an_activity():
        return await run_in_thread(release.wait, subject=SUBJECT)

    running = asyncio.create_task(environment.run(in_an_activity))
    await asyncio.sleep(0.3)
    release.set()
    await running
    assert len(heartbeats) >= 3


async def test_work_scheduled_without_a_heartbeat_timeout_is_run_in_a_thread_with_no_heartbeat():
    environment, heartbeats = ActivityEnvironment(), []
    environment.on_heartbeat = lambda *details: heartbeats.append(details)

    async def in_an_activity():
        return await run_in_thread(lambda: threading.current_thread().name, subject=SUBJECT)

    ran_in = await environment.run(in_an_activity)
    assert ran_in != threading.current_thread().name
    assert heartbeats == []


# ------------------------------------------------------------------ stored_size
def stored_size_in_an_activity(value) -> int:
    return ActivityEnvironment().run(stored_size, value)


def test_size_of_a_value_is_the_length_of_its_json_as_the_converter_writes_it():
    outcome = ActionOutcome("a", succeeded=True, result={}, error="")
    written = '{"action_id":"a","succeeded":true,"result":{},"error":""}'
    assert stored_size_in_an_activity(outcome) == len(written)


@pytest.mark.parametrize(
    "result",
    [{"x": float("nan")}, {"x": float("-inf")}, {"x": {1, 2}}, nested(5000)],
    ids=["nan", "infinity", "set", "deep"],
)
def test_value_json_cannot_hold_has_no_size(result):
    outcome = ActionOutcome("a", succeeded=True, result=result, error="")
    with pytest.raises(NotStorable):
        stored_size_in_an_activity(outcome)


# ------------------------------------------------------------------ checkpoints_in
def test_url_of_another_database_is_refused_without_repeating_the_password():
    with pytest.raises(DatabaseError, match="checkpoints need a PostgreSQL database") as refused:
        checkpoints_in(f"mysql://app:{PASSWORD}@localhost/db")
    assert PASSWORD not in str(refused.value)


def test_url_that_is_no_url_is_refused_without_repeating_it():
    with pytest.raises(DatabaseError, match="could not be parsed") as refused:
        checkpoints_in(f"{PASSWORD}:::not-a-url")
    assert PASSWORD not in str(refused.value)


def test_access_does_not_show_the_password_of_its_url():
    access = checkpoints_in(f"postgresql+psycopg://app:{PASSWORD}@localhost:1/db")
    assert PASSWORD not in repr(access)
    assert PASSWORD not in repr(access.of_attempt)
    assert PASSWORD not in repr(access.forget)


def test_attempt_holds_the_thread_and_another_is_refused_until_it_ends(checkpoints):
    with checkpoints.of_attempt("scripted-agent:held") as held:
        assert held.thread_id == "scripted-agent:held"
        with pytest.raises(AttemptInProgress), checkpoints.of_attempt("scripted-agent:held"):
            pass
    with checkpoints.of_attempt("scripted-agent:held"):
        pass


def test_forgetting_removes_the_checkpoints_of_the_thread_and_only_that_thread(checkpoints, engine):
    def stored(thread: str) -> int:
        with engine.connect() as conn:
            query = text("SELECT count(*) FROM checkpoints WHERE thread_id = :thread")
            return conn.execute(query, {"thread": thread}).scalar_one()

    for thread in ("scripted-agent:forgotten", "scripted-agent:kept"):
        with checkpoints.of_attempt(thread) as held:
            config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
            held.store.put(config, empty_checkpoint(), {"source": "loop", "step": 1}, {})
    checkpoints.forget("scripted-agent:forgotten")
    assert (stored("scripted-agent:forgotten"), stored("scripted-agent:kept")) == (0, 1)


# ------------------------------------------------------------------ forget_quietly
def an_access_that(forget) -> CheckpointAccess:
    def of_attempt(thread_id: str):
        raise AssertionError("not used")

    return CheckpointAccess(of_attempt=of_attempt, forget=forget)


def test_checkpoints_of_a_run_that_is_over_are_forgotten():
    forgotten: list[str] = []
    forget_quietly(an_access_that(forgotten.append), "campaign-run:7", subject=SUBJECT)
    assert forgotten == ["campaign-run:7"]


def test_forgetting_that_fails_is_logged_and_never_raised(caplog):
    def forget(thread_id: str) -> None:
        raise DatabaseError("connection is closed")

    forget_quietly(an_access_that(forget), "campaign-run:7", subject=SUBJECT)
    assert "checkpoints of campaign run 7 were not forgotten" in caplog.text
    assert "connection is closed" in caplog.text


def test_forgetting_with_a_database_that_cannot_be_reached_is_logged_without_the_password(caplog):
    unreachable = checkpoints_in(
        f"postgresql+psycopg://nobody:{PASSWORD}@127.0.0.1:1/nothing?connect_timeout=2"
    )
    forget_quietly(unreachable, "campaign-run:7", subject=SUBJECT)
    assert "were not forgotten" in caplog.text
    assert PASSWORD not in caplog.text
