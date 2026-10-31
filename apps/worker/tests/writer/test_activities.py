"""Tests for the activities the hooks of ``WritePost`` call, as plain functions on PostgreSQL.

``start_run`` marks a run started and answers the worker's limits; ``record_outcome`` settles
the post and marks the run succeeded, both or neither; ``record_failure`` records the run failed
with what its draft cost. Each is safe to repeat.
"""

import uuid
from decimal import Decimal

import pytest
from temporalio.exceptions import ApplicationError
from writer_fakes import QUICK

from openmarketer_core.db.models import ContentItemStatus, WritingRunStatus
from openmarketer_core.writing.workflow_contract import WritingRunInput
from openmarketer_worker.writer import steps


@pytest.fixture
def activities(writer_registration, activities_of):
    return activities_of(writer_registration())


async def with_a_stored_post(activities, writer_database) -> tuple:
    """A started run whose draft is stored, ``proposed``; the run and its item's id."""
    asked = writer_database.requested()
    activities.start(asked.run)
    request = (await activities.step(asked.run)).action
    stored = await activities.act(request)
    return asked.run, uuid.UUID(stored.result["content_item_id"])


async def with_a_post_recorded_as_sent(activities, writer_database) -> tuple:
    """A started run whose post a person approved and ``publish_post`` recorded, ``dry_run``."""
    asked = writer_database.requested()
    activities.start(asked.run)
    stored = await activities.act((await activities.step(asked.run)).action)
    request = (await activities.step(asked.run, stored)).action
    writer_database.held(asked.run, request)
    await activities.act(request)
    return asked.run, uuid.UUID(stored.result["content_item_id"])


def outcome(run, item_id, status: str, cost: str = "0.03") -> steps.RunOutcome:
    return steps.RunOutcome(run=run, content_item_id=item_id, status=status, cost_usd=cost)


def refused(activities, name: str, argument: object) -> ApplicationError:
    with pytest.raises(ApplicationError) as failure:
        activities.run(name, argument)
    return failure.value


# -------------------------------------------------------------------------------- start_run
def test_start_marks_the_run_running_and_answers_the_workers_limits(activities, writer_database):
    asked = writer_database.requested()

    answered = activities.start(asked.run)

    assert answered == QUICK
    stored = writer_database.run(asked.run)
    assert stored.status is WritingRunStatus.RUNNING and stored.profile_version_id is None


def test_start_of_a_run_that_is_not_there_yet_is_worth_another_attempt(activities):
    missing = WritingRunInput(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())

    failure = refused(activities, steps.START_RUN, missing)

    assert (failure.type, failure.non_retryable) == (steps.RUN_NOT_FOUND_YET, False)


def test_start_of_a_run_that_failed_is_refused_for_good(activities, writer_database):
    asked = writer_database.requested()
    activities.run(steps.RECORD_FAILURE, steps.RunFailure(run=asked.run, error="it broke"))

    failure = refused(activities, steps.START_RUN, asked.run)

    assert (failure.type, failure.non_retryable) == (steps.RUN_UNAVAILABLE, True)


# --------------------------------------------------------------------------- record_outcome
@pytest.mark.parametrize("status", [ContentItemStatus.REJECTED, ContentItemStatus.EXPIRED])
async def test_outcome_settles_the_post_and_marks_the_run_succeeded_with_its_cost(
    activities, writer_database, status
):
    run, item_id = await with_a_stored_post(activities, writer_database)

    activities.run(steps.RECORD_OUTCOME, outcome(run, item_id, status.value, "0.031"))

    stored = writer_database.run(run)
    assert (stored.status, stored.cost_usd) == (WritingRunStatus.SUCCEEDED, Decimal("0.031"))
    assert writer_database.item(run).status is status


async def test_outcome_of_a_post_recorded_as_sent_marks_the_run_succeeded(
    activities, writer_database
):
    run, item_id = await with_a_post_recorded_as_sent(activities, writer_database)

    activities.run(steps.RECORD_OUTCOME, outcome(run, item_id, "dry_run", "0.031"))

    stored = writer_database.run(run)
    assert (stored.status, stored.cost_usd) == (WritingRunStatus.SUCCEEDED, Decimal("0.031"))
    assert writer_database.item(run).status is ContentItemStatus.DRY_RUN


async def test_outcome_cannot_record_a_proposed_post_as_sent(activities, writer_database):
    run, item_id = await with_a_stored_post(activities, writer_database)

    failure = refused(activities, steps.RECORD_OUTCOME, outcome(run, item_id, "dry_run"))

    assert (failure.type, failure.non_retryable) == (steps.WRITING_FAILED, True)
    assert writer_database.item(run).status is ContentItemStatus.PROPOSED
    assert writer_database.run(run).status is WritingRunStatus.RUNNING


async def test_outcome_recorded_again_changes_nothing(activities, writer_database):
    run, item_id = await with_a_stored_post(activities, writer_database)
    activities.run(steps.RECORD_OUTCOME, outcome(run, item_id, "rejected"))
    finished_at = writer_database.run(run).finished_at

    activities.run(steps.RECORD_OUTCOME, outcome(run, item_id, "rejected"))

    assert writer_database.run(run).finished_at == finished_at


async def test_outcome_naming_an_item_that_is_not_the_runs_is_refused_and_settles_nothing(
    activities, writer_database
):
    run, item_id = await with_a_stored_post(activities, writer_database)
    other, others_item = await with_a_stored_post(activities, writer_database)

    failure = refused(activities, steps.RECORD_OUTCOME, outcome(run, others_item, "rejected"))

    assert (failure.type, failure.non_retryable) == (steps.RUN_UNAVAILABLE, True)
    assert writer_database.item(other).status is ContentItemStatus.PROPOSED
    assert writer_database.run(run).status is WritingRunStatus.RUNNING


async def test_outcome_of_a_run_that_failed_meanwhile_is_refused_for_good(
    activities, writer_database
):
    run, item_id = await with_a_stored_post(activities, writer_database)
    activities.run(steps.RECORD_FAILURE, steps.RunFailure(run=run, error="it broke"))

    failure = refused(activities, steps.RECORD_OUTCOME, outcome(run, item_id, "rejected"))

    assert (failure.type, failure.non_retryable) == (steps.RUN_UNAVAILABLE, True)
    assert writer_database.item(run).status is ContentItemStatus.FAILED


@pytest.mark.parametrize(
    ("status", "cost"),
    [
        ("failed", "0.03"),
        ("proposed", "0.03"),
        ("dry_run", "-1"),
        ("dry_run", "NaN"),
        ("dry_run", "a lot"),
    ],
)
async def test_outcome_a_run_cannot_succeed_with_is_refused_for_good(
    activities, writer_database, status, cost
):
    run, item_id = await with_a_stored_post(activities, writer_database)

    failure = refused(activities, steps.RECORD_OUTCOME, outcome(run, item_id, status, cost))

    assert (failure.type, failure.non_retryable) == (steps.WRITING_FAILED, True)
    assert writer_database.item(run).status is ContentItemStatus.PROPOSED


# --------------------------------------------------------------------------- record_failure
async def test_failure_records_the_reason_and_the_drafts_cost_and_settles_the_post_failed(
    activities, writer_database
):
    run, item_id = await with_a_stored_post(activities, writer_database)

    activities.run(steps.RECORD_FAILURE, steps.RunFailure(run=run, error="it broke"))

    stored = writer_database.run(run)
    assert (stored.status, stored.error) == (WritingRunStatus.FAILED, "it broke")
    assert stored.cost_usd == writer_database.item(run).cost_usd == Decimal("0.03")
    assert writer_database.item(run).status is ContentItemStatus.FAILED


def test_failure_of_a_run_that_stored_no_draft_is_recorded_at_no_cost(activities, writer_database):
    asked = writer_database.requested(approved=False)

    activities.run(steps.RECORD_FAILURE, steps.RunFailure(run=asked.run, error="no profile"))

    stored = writer_database.run(asked.run)
    assert (stored.status, stored.error, stored.cost_usd) == (
        WritingRunStatus.FAILED,
        "no profile",
        Decimal(0),
    )


async def test_failure_of_a_run_that_succeeded_records_nothing(activities, writer_database):
    run, item_id = await with_a_stored_post(activities, writer_database)
    activities.run(steps.RECORD_OUTCOME, outcome(run, item_id, "expired"))

    activities.run(steps.RECORD_FAILURE, steps.RunFailure(run=run, error="too late"))

    stored = writer_database.run(run)
    assert (stored.status, stored.error) == (WritingRunStatus.SUCCEEDED, None)


def test_failure_of_a_run_that_is_not_there_records_nothing(activities):
    missing = WritingRunInput(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())

    activities.run(steps.RECORD_FAILURE, steps.RunFailure(run=missing, error="gone"))
