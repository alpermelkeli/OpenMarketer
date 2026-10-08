"""Tests for the ``AnalyzeRepository`` workflow with scripted activities.

They show what the workflow does with each way an activity can end. They run
against the dev stack's Temporal server on a task queue of their own (see
``conftest.py``): the time-skipping test server is a download, and tests use
no network. Timeouts are therefore real, and short.
"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError
from temporalio.worker import Worker

from openmarketer_core.analysis_workflow import (
    ANALYZE_REPOSITORY_WORKFLOW,
    AnalyzeRepositoryInput,
    analysis_workflow_id,
)
from openmarketer_worker import steps
from openmarketer_worker.analyze_repository import AnalyzeRepository
from openmarketer_worker.policy import AnalysisPolicy

QUICK = AnalysisPolicy(
    attempt_timeout_seconds=20, heartbeat_timeout_seconds=10, max_attempts=2, retry_after_seconds=1
)


def failure(kind: str, message: str) -> ApplicationError:
    return ApplicationError(message, type=kind, non_retryable=kind in steps.NOT_RETRIED)


@dataclass
class ScriptedActivities:
    """Activities that do what a test tells them and remember what they were asked."""

    policy: AnalysisPolicy = QUICK
    start_outcome: Exception | None = None
    analysis_outcomes: list[Exception | float | None] = field(default_factory=list)
    started: list[AnalyzeRepositoryInput] = field(default_factory=list)
    analysed: list[AnalyzeRepositoryInput] = field(default_factory=list)
    recorded: list[steps.RunFailure] = field(default_factory=list)
    analysing: asyncio.Event = field(default_factory=asyncio.Event)

    @activity.defn(name=steps.START_RUN)
    async def start_run(self, run: AnalyzeRepositoryInput) -> AnalysisPolicy:
        self.started.append(run)
        if self.start_outcome is not None:
            raise self.start_outcome
        return self.policy

    @activity.defn(name=steps.ANALYSE_AND_STORE)
    async def analyse_and_store(self, run: AnalyzeRepositoryInput) -> None:
        """Each attempt takes the next outcome: an error, seconds to hang, or success."""
        self.analysed.append(run)
        self.analysing.set()
        outcome = self.analysis_outcomes.pop(0) if self.analysis_outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is not None:
            await asyncio.sleep(outcome)

    @activity.defn(name=steps.RECORD_FAILURE)
    async def record_failure(self, failed: steps.RunFailure) -> None:
        self.recorded.append(failed)


@dataclass
class Execution:
    client: Client
    task_queue: str

    async def start(self, run: AnalyzeRepositoryInput) -> WorkflowHandle:
        return await self.client.start_workflow(
            ANALYZE_REPOSITORY_WORKFLOW,
            run,
            id=analysis_workflow_id(run.run_id),
            task_queue=self.task_queue,
        )

    async def finish(self, run: AnalyzeRepositoryInput) -> None:
        await (await self.start(run)).result()

    async def fail(self, run: AnalyzeRepositoryInput) -> BaseException:
        with pytest.raises(WorkflowFailureError) as failed:
            await self.finish(run)
        assert failed.value.__cause__ is not None
        return failed.value.__cause__


@asynccontextmanager
async def serving(client: Client, task_queue: str, activities: ScriptedActivities):
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[AnalyzeRepository],
        activities=[activities.start_run, activities.analyse_and_store, activities.record_failure],
        activity_executor=ThreadPoolExecutor(max_workers=2),
    )
    async with worker:
        yield Execution(client, task_queue)


# -------------------------------------------------------------------- success
async def test_run_is_started_then_analysed_and_no_failure_is_recorded(
    temporal, task_queue, some_run
):
    activities, run = ScriptedActivities(), some_run()
    async with serving(temporal, task_queue, activities) as execution:
        await execution.finish(run)
    assert (activities.started, activities.analysed, activities.recorded) == ([run], [run], [])


async def test_history_holds_the_identifiers_of_the_run_and_nothing_else(
    temporal, task_queue, some_run
):
    run = some_run()
    async with serving(temporal, task_queue, ScriptedActivities()) as execution:
        handle = await execution.start(run)
        await handle.result()
    started = (await handle.fetch_history()).events[0]
    arguments = started.workflow_execution_started_event_attributes.input.payloads
    assert [json.loads(argument.data) for argument in arguments] == [
        {
            "workspace_id": str(run.workspace_id),
            "project_id": str(run.project_id),
            "run_id": str(run.run_id),
        }
    ]


# ---------------------------------------------------------- failures worth a retry
async def test_analysis_is_tried_again_after_a_failure_that_may_pass(
    temporal, task_queue, some_run
):
    limited = failure(steps.MODEL_UNAVAILABLE, "HTTP 429: rate limited")
    activities, run = ScriptedActivities(analysis_outcomes=[limited, None]), some_run()
    async with serving(temporal, task_queue, activities) as execution:
        await execution.finish(run)
    assert (activities.analysed, activities.recorded) == ([run, run], [])


async def test_failure_that_keeps_happening_is_recorded_once_attempts_are_used_up(
    temporal, task_queue, some_run
):
    limited = failure(steps.MODEL_UNAVAILABLE, "HTTP 429: rate limited")
    activities, run = ScriptedActivities(analysis_outcomes=[limited, limited, None]), some_run()
    async with serving(temporal, task_queue, activities) as execution:
        assert isinstance(await execution.fail(run), ActivityError)
    assert len(activities.analysed) == QUICK.max_attempts
    assert activities.recorded == [steps.RunFailure(run=run, error="HTTP 429: rate limited")]


async def test_number_of_attempts_is_what_the_worker_configured(temporal, task_queue, some_run):
    limited = failure(steps.MODEL_UNAVAILABLE, "HTTP 429: rate limited")
    once = AnalysisPolicy(max_attempts=1)
    activities = ScriptedActivities(policy=once, analysis_outcomes=[limited, None])
    async with serving(temporal, task_queue, activities) as execution:
        await execution.fail(some_run())
    assert len(activities.analysed) == 1


# ------------------------------------------------------ failures not worth a retry
@pytest.mark.parametrize("kind", steps.NOT_RETRIED)
async def test_refused_analysis_is_not_tried_again_and_its_reason_is_recorded(
    temporal, task_queue, some_run, kind
):
    refused = failure(kind, "git clone failed: repository not found")
    activities, run = ScriptedActivities(analysis_outcomes=[refused, None]), some_run()
    async with serving(temporal, task_queue, activities) as execution:
        await execution.fail(run)
    assert activities.analysed == [run]
    assert activities.recorded == [
        steps.RunFailure(run=run, error="git clone failed: repository not found")
    ]


async def test_run_that_cannot_be_started_is_not_analysed(temporal, task_queue, some_run):
    finished = failure(steps.RUN_UNAVAILABLE, "analysis run has already failed")
    activities, run = ScriptedActivities(start_outcome=finished), some_run()
    async with serving(temporal, task_queue, activities) as execution:
        await execution.fail(run)
    assert (len(activities.started), activities.analysed) == (1, [])
    assert activities.recorded == [
        steps.RunFailure(run=run, error="analysis run has already failed")
    ]


async def test_failure_nobody_worded_is_recorded_without_its_message(
    temporal, task_queue, some_run
):
    once = AnalysisPolicy(max_attempts=1)
    leaking = ScriptedActivities(policy=once, analysis_outcomes=[ValueError("/tmp/clone-1234")])
    async with serving(temporal, task_queue, leaking) as execution:
        await execution.fail(some_run())
    assert [failed.error for failed in leaking.recorded] == [steps.STOPPED_UNEXPECTEDLY]


# ----------------------------------------------------- timeouts and cancellation
async def test_attempt_that_takes_too_long_is_recorded_as_timed_out(temporal, task_queue, some_run):
    impatient = AnalysisPolicy(
        attempt_timeout_seconds=1, heartbeat_timeout_seconds=10, max_attempts=1
    )
    hanging = ScriptedActivities(policy=impatient, analysis_outcomes=[5])
    async with serving(temporal, task_queue, hanging) as execution:
        await execution.fail(some_run())
    assert [failed.error for failed in hanging.recorded] == [steps.TIMED_OUT]


async def test_worker_that_stops_sending_heartbeats_is_noticed_before_the_attempt_ends(
    temporal, task_queue, some_run
):
    silent = AnalysisPolicy(attempt_timeout_seconds=60, heartbeat_timeout_seconds=1, max_attempts=1)
    hanging = ScriptedActivities(policy=silent, analysis_outcomes=[5])
    async with serving(temporal, task_queue, hanging) as execution:
        await execution.fail(some_run())
    assert [failed.error for failed in hanging.recorded] == [steps.WORKER_STOPPED]


async def test_attempt_lost_with_its_worker_is_made_again(temporal, task_queue, some_run):
    silent = AnalysisPolicy(
        attempt_timeout_seconds=60,
        heartbeat_timeout_seconds=1,
        max_attempts=2,
        retry_after_seconds=1,
    )
    activities, run = ScriptedActivities(policy=silent, analysis_outcomes=[5, None]), some_run()
    async with serving(temporal, task_queue, activities) as execution:
        await execution.finish(run)
    assert (activities.analysed, activities.recorded) == ([run, run], [])


async def test_cancelled_workflow_still_records_the_failure(temporal, task_queue, some_run):
    activities, run = ScriptedActivities(analysis_outcomes=[30]), some_run()
    async with serving(temporal, task_queue, activities) as execution:
        handle = await execution.start(run)
        await asyncio.wait_for(activities.analysing.wait(), timeout=20)
        await handle.cancel()
        with pytest.raises(WorkflowFailureError) as stopped:
            await handle.result()
    assert isinstance(stopped.value.__cause__, CancelledError)
    assert activities.recorded == [steps.RunFailure(run=run, error=steps.CANCELLED)]
