"""Tests for the schedule that runs the checkpoint cleanups it is given.

The cleanups here are stand-ins: the schedule knows no rule, and the rules are
tested in core. A session is opened for each, so they need PostgreSQL (see the
root ``conftest.py``).
"""

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from openmarketer_worker.checkpoint_cleanup import (
    CheckpointCleanup,
    keep_removing_leftover_checkpoints,
    remove_leftover_checkpoints,
)

LOGGER = "openmarketer_worker.checkpoint_cleanup"


class Counting:
    """A cleanup that removes nothing and counts how often it ran."""

    def __init__(self, removes: int = 0) -> None:
        self.rounds = 0
        self.removes = removes

    def __call__(self, session: Session) -> int:
        session.execute(text("SELECT 1"))
        self.rounds += 1
        return self.removes


def database_fails(session: Session) -> int:
    raise OperationalError("DELETE FROM checkpoints", {}, Exception("server closed the connection"))


def has_a_defect(session: Session) -> int:
    raise KeyError("thread_id")


def test_every_cleanup_runs_in_a_round(sessions):
    plans, drafts = Counting(), Counting()
    remove_leftover_checkpoints(
        sessions, [CheckpointCleanup("plans", plans), CheckpointCleanup("drafts", drafts)]
    )
    assert (plans.rounds, drafts.rounds) == (1, 1)


def test_each_cleanup_has_a_transaction_of_its_own(sessions):
    seen: list[Session] = []

    def remember(session: Session) -> int:
        seen.append(session)
        return 0

    remove_leftover_checkpoints(
        sessions, [CheckpointCleanup("plans", remember), CheckpointCleanup("drafts", remember)]
    )
    first, second = seen
    assert first is not second


def test_cleanup_that_meets_a_database_error_does_not_keep_the_next_from_running(sessions):
    drafts = Counting()
    remove_leftover_checkpoints(
        sessions,
        [CheckpointCleanup("plans", database_fails), CheckpointCleanup("drafts", drafts)],
    )
    assert drafts.rounds == 1


def test_cleanup_with_a_defect_does_not_keep_the_next_from_running(sessions):
    drafts = Counting()
    remove_leftover_checkpoints(
        sessions, [CheckpointCleanup("plans", has_a_defect), CheckpointCleanup("drafts", drafts)]
    )
    assert drafts.rounds == 1


def test_failure_is_logged_with_the_runs_the_cleanup_is_for(sessions, caplog):
    remove_leftover_checkpoints(sessions, [CheckpointCleanup("plans", database_fails)])
    assert "left-over checkpoints of plans were not removed" in caplog.text


def test_log_says_how_many_runs_and_names_them_as_the_cleanup_does(sessions, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        remove_leftover_checkpoints(sessions, [CheckpointCleanup("plans", Counting(removes=2))])
    assert caplog.messages == ["removed the left-over checkpoints of 2 plans"]


def test_round_that_removed_nothing_logs_nothing(sessions, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        remove_leftover_checkpoints(sessions, [CheckpointCleanup("plans", Counting())])
    assert caplog.messages == []


def test_round_with_no_cleanups_does_nothing(sessions, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        remove_leftover_checkpoints(sessions, [])
    assert caplog.messages == []


async def test_schedule_with_no_cleanups_ends_at_once(sessions):
    await asyncio.wait_for(
        keep_removing_leftover_checkpoints(sessions, [], every_seconds=3600), timeout=5
    )


async def test_schedule_runs_every_cleanup_again_after_the_interval(sessions):
    plans, drafts = Counting(), Counting()
    schedule = asyncio.create_task(
        keep_removing_leftover_checkpoints(
            sessions,
            [CheckpointCleanup("plans", plans), CheckpointCleanup("drafts", drafts)],
            every_seconds=0.02,
        )
    )
    try:
        await asyncio.wait_for(rounds_reach(2, plans, drafts), timeout=10)
    finally:
        schedule.cancel()
        await asyncio.gather(schedule, return_exceptions=True)
    assert min(plans.rounds, drafts.rounds) >= 2


async def test_schedule_goes_on_after_a_round_in_which_a_cleanup_failed(sessions):
    drafts = Counting()
    schedule = asyncio.create_task(
        keep_removing_leftover_checkpoints(
            sessions,
            [CheckpointCleanup("plans", has_a_defect), CheckpointCleanup("drafts", drafts)],
            every_seconds=0.02,
        )
    )
    try:
        await asyncio.wait_for(rounds_reach(2, drafts), timeout=10)
    finally:
        schedule.cancel()
        await asyncio.gather(schedule, return_exceptions=True)
    assert drafts.rounds >= 2


async def rounds_reach(rounds: int, *cleanups: Counting) -> None:
    """Returns once every cleanup ran that often; the caller limits the wait."""
    while any(cleanup.rounds < rounds for cleanup in cleanups):
        await asyncio.sleep(0.01)
