"""Tests for how the ``Research`` workflow tells the chat that asked for a run how it ended.

The activities are scripted under the names the worker registers the real ones with, and run
against the dev stack's Temporal server on a task queue of the test's own (see ``conftest.py``
of the worker's tests); they are skipped when it is not reachable. Timeouts are real, so the
limits are short.

The follow-up is a child the workflow starts and abandons on the orchestrator's queue. These
tests serve no worker there: a child is started whether or not anything takes it, and the
tests read the child Temporal holds under the follow-up's id, then terminate it.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from openmarketer_core.agent_runtime.steps import GraphRunRef, StepOutcome
from openmarketer_core.research.workflow_contract import (
    RESEARCH_RUN_KIND,
    RESEARCH_WORKFLOW,
    ResearchRunInput,
    research_workflow_id,
)
from openmarketer_worker.agent_runtime.vocabulary import FORGET_RUN, RUN_GRAPH_STEP, StepInput
from openmarketer_worker.orchestrator.contract import (
    CHAT_FOLLOW_UP_TASK_QUEUE,
    CHAT_FOLLOW_UP_WORKFLOW,
    follow_up_workflow_id,
)
from openmarketer_worker.researcher import steps
from openmarketer_worker.researcher.policy import ResearchPolicy
from openmarketer_worker.researcher.workflow import Research

QUICK = ResearchPolicy(
    attempt_timeout_seconds=20, heartbeat_timeout_seconds=10, max_attempts=2, retry_after_seconds=1
)
REPORTED = {"report_id": "8c1f8a52-4f0e-4a3b-9d53-0a3f5b9c2e11", "findings": 3}


def failure(kind: str, message: str) -> ApplicationError:
    return ApplicationError(message, type=kind, non_retryable=kind in steps.NOT_RETRIED)


@dataclass
class ScriptedActivities:
    """Activities that do what a test tells them and remember what they were asked.

    Each attempt at a step takes the next of ``step_outcomes`` (an error to raise) and, once
    they are used up, finishes with ``output``. ``record_end`` answers with ``chat_id`` unless
    it is given ``end_outcome`` to raise.
    """

    chat_id: uuid.UUID | None = None
    output: dict = field(default_factory=lambda: dict(REPORTED))
    step_outcomes: list[Exception] = field(default_factory=list)
    end_outcome: Exception | None = None
    failure_outcome: Exception | None = None
    steps_taken: int = 0
    recorded: list[steps.RunFailure] = field(default_factory=list)
    ended: list[steps.RunEnd] = field(default_factory=list)
    forgotten: list[GraphRunRef] = field(default_factory=list)

    @activity.defn(name=steps.START_RUN)
    async def start_run(self, run: ResearchRunInput) -> ResearchPolicy:
        return QUICK

    @activity.defn(name=RUN_GRAPH_STEP)
    async def run_graph_step(self, step: StepInput) -> StepOutcome:
        self.steps_taken += 1
        if self.step_outcomes:
            raise self.step_outcomes.pop(0)
        return StepOutcome.finished(self.output)

    @activity.defn(name=steps.RECORD_FAILURE)
    async def record_failure(self, failed: steps.RunFailure) -> None:
        self.recorded.append(failed)
        if self.failure_outcome is not None:
            raise self.failure_outcome

    @activity.defn(name=steps.RECORD_END)
    async def record_end(self, end: steps.RunEnd) -> steps.RunEndRecorded:
        self.ended.append(end)
        if self.end_outcome is not None:
            raise self.end_outcome
        return steps.RunEndRecorded(chat_id=self.chat_id)

    @activity.defn(name=FORGET_RUN)
    async def forget_run(self, run: GraphRunRef) -> None:
        self.forgotten.append(run)


@dataclass
class Execution:
    client: Client
    task_queue: str

    async def start(self, run: ResearchRunInput) -> WorkflowHandle:
        return await self.client.start_workflow(
            RESEARCH_WORKFLOW, run, id=research_workflow_id(run.run_id), task_queue=self.task_queue
        )

    async def finish(self, run: ResearchRunInput) -> object:
        # A workflow task that cannot be decoded is tried again for ever: a deadline makes a
        # run that hangs a failure of the test.
        return await asyncio.wait_for((await self.start(run)).result(), timeout=60)

    async def fail(self, run: ResearchRunInput) -> BaseException:
        with pytest.raises(WorkflowFailureError) as failed:
            await self.finish(run)
        assert failed.value.__cause__ is not None
        return failed.value.__cause__

    async def scheduled(self, run: ResearchRunInput) -> list[str]:
        """The names of the activities the run's workflow scheduled, in the order it did."""
        history = await self.client.get_workflow_handle(
            research_workflow_id(run.run_id)
        ).fetch_history()
        return [
            event.activity_task_scheduled_event_attributes.activity_type.name
            for event in history.events
            if event.HasField("activity_task_scheduled_event_attributes")
        ]


@dataclass(frozen=True)
class FollowUp:
    """A follow-up Temporal holds: its type, its queue, and the input it was started with."""

    workflow_type: str
    task_queue: str
    input: dict
    run_id: str


@pytest.fixture
async def follow_ups(temporal) -> AsyncIterator[Callable]:
    """Reads the follow-up of a research run, if one was started; and ends each one read."""
    read: list[str] = []

    async def follow_up_of(run: ResearchRunInput) -> FollowUp | None:
        handle = temporal.get_workflow_handle(follow_up_workflow_id(RESEARCH_RUN_KIND, run.run_id))
        try:
            described = await handle.describe()
        except RPCError as e:
            if e.status is RPCStatusCode.NOT_FOUND:
                return None
            raise
        read.append(handle.id)
        started = (await handle.fetch_history()).events[0]
        (argument,) = started.workflow_execution_started_event_attributes.input.payloads
        return FollowUp(
            workflow_type=described.workflow_type,
            task_queue=described.task_queue,
            input=json.loads(argument.data),
            run_id=described.run_id,
        )

    yield follow_up_of
    for workflow_id in read:
        try:
            await temporal.get_workflow_handle(workflow_id).terminate()
        except RPCError:
            pass  # it ended already: a worker of the dev stack may serve the queue


@asynccontextmanager
async def serving(client: Client, task_queue: str, activities: ScriptedActivities):
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[Research],
        activities=[
            activities.start_run,
            activities.run_graph_step,
            activities.record_failure,
            activities.record_end,
            activities.forget_run,
        ],
    )
    async with worker:
        yield Execution(client, task_queue)


def a_run() -> ResearchRunInput:
    return ResearchRunInput(workspace_id=uuid.uuid4(), project_id=uuid.uuid4(), run_id=uuid.uuid4())


# -------------------------------------------------------------------- success
async def test_run_that_finished_records_its_end_before_it_is_forgotten(temporal, task_queue):
    activities, run = ScriptedActivities(), a_run()
    async with serving(temporal, task_queue, activities) as execution:
        await execution.finish(run)
        scheduled = await execution.scheduled(run)
    assert scheduled == [steps.START_RUN, RUN_GRAPH_STEP, steps.RECORD_END, FORGET_RUN]


async def test_run_that_finished_tells_how_many_findings_its_report_holds(temporal, task_queue):
    activities, run = ScriptedActivities(), a_run()
    async with serving(temporal, task_queue, activities) as execution:
        assert await execution.finish(run) == REPORTED
    assert activities.ended == [steps.RunEnd(run=run, findings=3)]


async def test_run_whose_output_has_no_count_tells_its_end_without_one(temporal, task_queue):
    activities, run = ScriptedActivities(output={"report_id": "r"}), a_run()
    async with serving(temporal, task_queue, activities) as execution:
        await execution.finish(run)
    assert activities.ended == [steps.RunEnd(run=run, findings=None)]


async def test_run_asked_for_by_a_chat_starts_the_chats_follow_up(temporal, task_queue, follow_ups):
    chat_id, run = uuid.uuid4(), a_run()
    async with serving(temporal, task_queue, ScriptedActivities(chat_id=chat_id)) as execution:
        await execution.finish(run)
    followed = await follow_ups(run)
    assert followed is not None
    assert (followed.workflow_type, followed.task_queue) == (
        CHAT_FOLLOW_UP_WORKFLOW,
        CHAT_FOLLOW_UP_TASK_QUEUE,
    )
    assert followed.input == {
        "workspace_id": str(run.workspace_id),
        "project_id": str(run.project_id),
        "chat_id": str(chat_id),
        "run_kind": RESEARCH_RUN_KIND,
        "run_id": str(run.run_id),
    }


async def test_run_asked_for_by_no_chat_starts_no_follow_up(temporal, task_queue, follow_ups):
    run = a_run()
    async with serving(temporal, task_queue, ScriptedActivities(chat_id=None)) as execution:
        await execution.finish(run)
    assert await follow_ups(run) is None


async def test_run_told_twice_follows_up_once_and_finishes_both_times(
    temporal, task_queue, follow_ups
):
    run = a_run()
    async with serving(temporal, task_queue, ScriptedActivities(chat_id=uuid.uuid4())) as execution:
        await execution.finish(run)
        first = await follow_ups(run)
        assert await execution.finish(run) == REPORTED
    second = await follow_ups(run)
    assert first is not None and second is not None
    assert second.run_id == first.run_id


# ---------------------------------------------------------- failures worth a retry
async def test_run_that_finished_after_a_failure_that_passed_tells_it_finished(
    temporal, task_queue, follow_ups
):
    limited = failure(steps.MODEL_UNAVAILABLE, "HTTP 429: rate limited")
    activities = ScriptedActivities(chat_id=uuid.uuid4(), step_outcomes=[limited])
    run = a_run()
    async with serving(temporal, task_queue, activities) as execution:
        await execution.finish(run)
    assert (activities.steps_taken, activities.recorded) == (2, [])
    assert activities.ended == [steps.RunEnd(run=run, findings=3)]
    assert await follow_ups(run) is not None


# ------------------------------------------------------------- failures that end it
async def test_run_that_failed_tells_its_chat_why_after_recording_it(
    temporal, task_queue, follow_ups
):
    refused = failure(steps.RESEARCH_FAILED, "the stored request does not fit its schema")
    activities = ScriptedActivities(chat_id=uuid.uuid4(), step_outcomes=[refused])
    run = a_run()
    async with serving(temporal, task_queue, activities) as execution:
        await execution.fail(run)
        scheduled = await execution.scheduled(run)
    assert scheduled == [
        steps.START_RUN,
        RUN_GRAPH_STEP,
        steps.RECORD_FAILURE,
        steps.RECORD_END,
        FORGET_RUN,
    ]
    reason = "the stored request does not fit its schema"
    assert activities.ended == [steps.RunEnd(run=run, reason=reason)]
    assert await follow_ups(run) is not None


async def test_run_that_failed_tells_its_chat_in_the_researchers_words(temporal, task_queue):
    activities = ScriptedActivities(
        step_outcomes=[ApplicationError("a host", type="SomethingNobodyWorded", non_retryable=True)]
    )
    run = a_run()
    async with serving(temporal, task_queue, activities) as execution:
        await execution.fail(run)
    assert activities.ended == [
        steps.RunEnd(run=run, reason=steps.RESEARCH_TEXTS.stopped_unexpectedly)
    ]


async def test_run_whose_failure_was_not_recorded_tells_no_chat(temporal, task_queue, follow_ups):
    refused = failure(steps.RESEARCH_FAILED, "refused")
    activities = ScriptedActivities(
        chat_id=uuid.uuid4(),
        step_outcomes=[refused],
        failure_outcome=failure(steps.RUN_UNAVAILABLE, "there is no such run"),
    )
    run = a_run()
    async with serving(temporal, task_queue, activities) as execution:
        await execution.fail(run)
    assert (activities.ended, await follow_ups(run)) == ([], None)


# ------------------------------------------- telling the chat never changes the outcome
async def test_run_that_finished_finishes_when_its_end_cannot_be_recorded(
    temporal, task_queue, follow_ups
):
    activities = ScriptedActivities(
        chat_id=uuid.uuid4(), end_outcome=failure(steps.RUN_UNAVAILABLE, "there is no such run")
    )
    run = a_run()
    async with serving(temporal, task_queue, activities) as execution:
        assert await execution.finish(run) == REPORTED
    assert (activities.recorded, await follow_ups(run)) == ([], None)
    assert activities.forgotten != []


async def test_run_that_failed_fails_with_its_own_reason_when_its_end_cannot_be_recorded(
    temporal, task_queue
):
    refused = failure(steps.RESEARCH_FAILED, "the stored request does not fit its schema")
    activities = ScriptedActivities(
        step_outcomes=[refused], end_outcome=failure(steps.RUN_UNAVAILABLE, "no such run")
    )
    run = a_run()
    async with serving(temporal, task_queue, activities) as execution:
        cause = await execution.fail(run)
    assert isinstance(cause.__cause__, ApplicationError)
    assert cause.__cause__.type == steps.RESEARCH_FAILED
    assert activities.forgotten != []
