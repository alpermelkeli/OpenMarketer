"""Tests for what activities of any agent owe the engine: worded failures, heartbeats, checkpoints.

Heartbeats and cancellation run in Temporal's activity environment, which needs no server;
the checkpoints are the real ones in PostgreSQL, except where the database is made unreachable.
"""

import asyncio
import logging
import threading

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from scripted_work import happened
from sqlalchemy import text
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from openmarketer_core.db.session import DatabaseError
from openmarketer_core.graph_checkpoints.postgres import AttemptInProgress
from openmarketer_worker.agent_runtime.activity_support import (
    CheckpointAccess,
    checkpoints_in,
    failure,
    forget_quietly,
    run_with_heartbeats,
    worded_failures,
)
from openmarketer_worker.agent_runtime.vocabulary import (
    DATABASE_UNAVAILABLE,
    UNEXPECTED,
    FailureTexts,
)

# An agent's own words: the helpers hold none.
ANALYSIS_TEXTS = FailureTexts(
    stopped_unexpectedly="the analysis stopped unexpectedly",
    timed_out="the analysis did not finish in time",
    worker_stopped="the worker running the analysis stopped responding",
    cancelled="the analysis was cancelled",
    database_failed="the database could not be reached",
)
SUBJECT = "analysis run 7"
PASSWORD = "hunter2"


# ------------------------------------------------------------------ failure
def test_failure_has_the_type_and_message_it_was_given_and_is_tried_again_by_default():
    error = failure("ModelUnavailable", "the model provider answered HTTP 429")
    assert (error.type, error.message, error.non_retryable) == (
        "ModelUnavailable",
        "the model provider answered HTTP 429",
        False,
    )


def test_failure_can_be_for_good():
    assert failure("AnalysisFailed", "no profile", non_retryable=True).non_retryable


# ------------------------------------------------------------------ worded_failures
def leaving_an_activity(error: BaseException) -> ApplicationError:
    """What leaves an activity that raised ``error`` inside ``worded_failures``."""
    with pytest.raises(ApplicationError) as raised:
        with worded_failures(ANALYSIS_TEXTS, subject=SUBJECT):
            raise error
    return raised.value


def test_failure_an_activity_worded_passes_as_it_is():
    worded = failure("AnalysisFailed", "no profile", non_retryable=True)
    assert leaving_an_activity(worded) is worded


def test_failure_of_the_database_becomes_one_worth_another_attempt_in_the_agents_words(caplog):
    error = leaving_an_activity(DatabaseError(f"connection to host with password {PASSWORD} lost"))
    assert (error.type, error.message, error.non_retryable) == (
        DATABASE_UNAVAILABLE,
        "the database could not be reached",
        False,
    )
    assert error.__cause__ is None
    assert "analysis run 7: the database could not be used" in caplog.text
    assert PASSWORD in caplog.text  # the detail is for the log, not for the run


def test_error_nobody_worded_becomes_an_unexpected_failure_without_its_detail(caplog):
    error = leaving_an_activity(ValueError(f"/Users/someone/clone-1234 {PASSWORD}"))
    assert (error.type, error.message, error.non_retryable) == (
        UNEXPECTED,
        "the analysis stopped unexpectedly",
        False,
    )
    assert error.__cause__ is None
    assert PASSWORD not in str(error)
    assert "analysis run 7 stopped on an unexpected error" in caplog.text
    assert "/Users/someone/clone-1234" in caplog.text


def test_cancellation_is_not_an_error_to_word():
    with pytest.raises(asyncio.CancelledError), worded_failures(ANALYSIS_TEXTS, subject=SUBJECT):
        raise asyncio.CancelledError


def test_block_that_works_is_left_alone():
    with worded_failures(ANALYSIS_TEXTS, subject=SUBJECT):
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
    assert "analysis run 7 was cancelled while its work was running" in caplog.text


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


def test_attempt_holds_the_thread_and_another_is_refused_until_it_ends(checkpoint_access):
    with checkpoint_access.of_attempt("scripted-agent:held") as held:
        assert held.thread_id == "scripted-agent:held"
        with pytest.raises(AttemptInProgress), checkpoint_access.of_attempt("scripted-agent:held"):
            pass
    with checkpoint_access.of_attempt("scripted-agent:held"):
        pass


def test_forgetting_removes_the_checkpoints_of_the_thread_and_only_that_thread(
    checkpoint_access, engine
):
    def stored(thread: str) -> int:
        with engine.connect() as conn:
            query = text("SELECT count(*) FROM checkpoints WHERE thread_id = :thread")
            return conn.execute(query, {"thread": thread}).scalar_one()

    for thread in ("scripted-agent:forgotten", "scripted-agent:kept"):
        with checkpoint_access.of_attempt(thread) as checkpoints:
            config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
            checkpoints.store.put(config, empty_checkpoint(), {"source": "loop", "step": 1}, {})
    checkpoint_access.forget("scripted-agent:forgotten")
    assert (stored("scripted-agent:forgotten"), stored("scripted-agent:kept")) == (0, 1)


# ------------------------------------------------------------------ forget_quietly
def an_access_that(forget) -> CheckpointAccess:
    def of_attempt(thread_id: str):
        raise AssertionError("not used")

    return CheckpointAccess(of_attempt=of_attempt, forget=forget)


def test_checkpoints_of_a_run_that_is_over_are_forgotten():
    forgotten: list[str] = []
    forget_quietly(an_access_that(forgotten.append), "analysis-run:7", subject=SUBJECT)
    assert forgotten == ["analysis-run:7"]


def test_forgetting_that_fails_is_logged_and_never_raised(caplog):
    def forget(thread_id: str) -> None:
        raise DatabaseError("connection is closed")

    forget_quietly(an_access_that(forget), "analysis-run:7", subject=SUBJECT)
    assert "checkpoints of analysis run 7 were not forgotten" in caplog.text
    assert "connection is closed" in caplog.text


def test_forgetting_with_a_database_that_cannot_be_reached_is_logged_without_the_password(caplog):
    unreachable = checkpoints_in(
        f"postgresql+psycopg://nobody:{PASSWORD}@127.0.0.1:1/nothing?connect_timeout=2"
    )
    forget_quietly(unreachable, "analysis-run:7", subject=SUBJECT)
    assert "were not forgotten" in caplog.text
    assert PASSWORD not in caplog.text
