"""Tests for the ``AnalyzeRepository`` workflow with scripted activities.

They show what the workflow does with each way an activity can end: which activity follows,
what a run is told, and what Temporal was asked to run each one under. The activities are
scripted under the names the worker registers the real ones with. They run against the dev
stack's Temporal server on a task queue of their own (see ``conftest.py`` of the worker's
tests): the time-skipping test server is a download, and tests use no network. Timeouts are
therefore real, and short.
"""

import asyncio
import json
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle, WorkflowHistory
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError
from temporalio.worker import Worker

from openmarketer_core.agent_runtime.actions import ActionOutcome, ActionRequest
from openmarketer_core.agent_runtime.steps import GraphRunRef, StepOutcome
from openmarketer_core.repository_analysis.analyzer_agent.definitions import CLONE_REPOSITORY
from openmarketer_core.repository_analysis.workflow_contract import (
    ANALYSIS_RUN_KIND,
    ANALYZE_REPOSITORY_WORKFLOW,
    AnalyzeRepositoryInput,
    analysis_workflow_id,
)
from openmarketer_worker.agent_runtime import vocabulary
from openmarketer_worker.agent_runtime.vocabulary import (
    ATTEMPT_STILL_RUNNING,
    FORGET_RUN,
    RUN_ACTION,
    RUN_GRAPH_STEP,
    StepInput,
)
from openmarketer_worker.repository_analyzer import steps
from openmarketer_worker.repository_analyzer.policy import AnalysisPolicy
from openmarketer_worker.repository_analyzer.workflow import AnalyzeRepository

QUICK = AnalysisPolicy(
    attempt_timeout_seconds=20, heartbeat_timeout_seconds=10, max_attempts=2, retry_after_seconds=1
)
STORED = {"profile_id": "8c1f8a52-4f0e-4a3b-9d53-0a3f5b9c2e11"}
CLONED = {"commit_sha": "a" * 40}


def failure(kind: str, message: str) -> ApplicationError:
    return ApplicationError(message, type=kind, non_retryable=kind in steps.NOT_RETRIED)


@dataclass
class ScriptedActivities:
    """Activities that do what a test tells them and remember what they were asked.

    The graph's steps do what the analyzer's do: the first asks for the clone, and the one
    that is given a clone that failed ends the analysis with what it was told.
    """

    policy: AnalysisPolicy = QUICK
    start_outcome: Exception | None = None
    clone_outcome: Exception | None = None
    analysis_outcomes: list[Exception | float | None] = field(default_factory=list)
    started: list[AnalyzeRepositoryInput] = field(default_factory=list)
    cloned: list[ActionRequest] = field(default_factory=list)
    analysed: list[ActionOutcome] = field(default_factory=list)  # what each attempt was told
    recorded: list[steps.RunFailure] = field(default_factory=list)
    forgotten: list[GraphRunRef] = field(default_factory=list)
    analysing: asyncio.Event = field(default_factory=asyncio.Event)

    @activity.defn(name=steps.START_RUN)
    async def start_run(self, run: AnalyzeRepositoryInput) -> AnalysisPolicy:
        self.started.append(run)
        if self.start_outcome is not None:
            raise self.start_outcome
        return self.policy

    @activity.defn(name=RUN_GRAPH_STEP)
    async def run_graph_step(self, step: StepInput) -> StepOutcome:
        """After the clone, each attempt takes the next outcome: an error, seconds to hang,
        or success."""
        if step.resume is None:
            run_id = str(step.run.run_id)
            return StepOutcome.waiting_for(
                ActionRequest(
                    action_id=f"{run_id}:clone", name=CLONE_REPOSITORY, payload={"run_id": run_id}
                )
            )
        self.analysed.append(step.resume)
        self.analysing.set()
        if not step.resume.succeeded:
            raise failure(steps.ANALYSIS_FAILED, step.resume.error)
        outcome = self.analysis_outcomes.pop(0) if self.analysis_outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is not None:
            await asyncio.sleep(outcome)
        return StepOutcome.finished(STORED)

    @activity.defn(name=RUN_ACTION)
    async def run_action(self, request: ActionRequest) -> ActionOutcome:
        self.cloned.append(request)
        if self.clone_outcome is not None:
            raise self.clone_outcome
        return ActionOutcome(request.action_id, succeeded=True, result=CLONED, error="")

    @activity.defn(name=steps.RECORD_FAILURE)
    async def record_failure(self, failed: steps.RunFailure) -> None:
        self.recorded.append(failed)

    @activity.defn(name=FORGET_RUN)
    async def forget_run(self, run: GraphRunRef) -> None:
        self.forgotten.append(run)


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

    async def finish(self, run: AnalyzeRepositoryInput) -> object:
        # A workflow task that cannot be decoded is tried again for ever: a deadline makes a
        # run that hangs a failure of the test.
        return await asyncio.wait_for((await self.start(run)).result(), timeout=60)

    async def fail(self, run: AnalyzeRepositoryInput) -> BaseException:
        with pytest.raises(WorkflowFailureError) as failed:
            await self.finish(run)
        assert failed.value.__cause__ is not None
        return failed.value.__cause__

    async def history(self, run: AnalyzeRepositoryInput) -> WorkflowHistory:
        return await self.client.get_workflow_handle(
            analysis_workflow_id(run.run_id)
        ).fetch_history()


@asynccontextmanager
async def serving(client: Client, task_queue: str, activities: ScriptedActivities):
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[AnalyzeRepository],
        activities=[
            activities.start_run,
            activities.run_graph_step,
            activities.run_action,
            activities.record_failure,
            activities.forget_run,
        ],
    )
    async with worker:
        yield Execution(client, task_queue)


def graph_run(run: AnalyzeRepositoryInput) -> GraphRunRef:
    return GraphRunRef(kind=ANALYSIS_RUN_KIND, run_id=run.run_id)


def scheduled(history: WorkflowHistory) -> list[str]:
    """The names of the activities the workflow scheduled, in the order it did."""
    return [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]


@dataclass(frozen=True)
class Limits:
    """What Temporal was asked to run an activity under."""

    start_to_close: timedelta
    heartbeat: timedelta
    max_attempts: int
    first_wait: timedelta


def limits_of(history: WorkflowHistory, name: str) -> Limits:
    """The limits the first scheduling of the activity ``name`` asked for."""
    asked = next(
        event.activity_task_scheduled_event_attributes
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
        and event.activity_task_scheduled_event_attributes.activity_type.name == name
    )
    return Limits(
        start_to_close=asked.start_to_close_timeout.ToTimedelta(),
        heartbeat=asked.heartbeat_timeout.ToTimedelta(),
        max_attempts=asked.retry_policy.maximum_attempts,
        first_wait=asked.retry_policy.initial_interval.ToTimedelta(),
    )


# -------------------------------------------------------------------- success
async def test_run_is_started_cloned_analysed_and_forgotten_in_that_order(
    temporal, task_queue, some_run
):
    activities, run = ScriptedActivities(), some_run()
    async with serving(temporal, task_queue, activities) as execution:
        await execution.finish(run)
        history = await execution.history(run)
    assert scheduled(history) == [
        steps.START_RUN,
        RUN_GRAPH_STEP,
        RUN_ACTION,
        RUN_GRAPH_STEP,
        FORGET_RUN,
    ]
    assert (activities.started, activities.recorded) == ([run], [])
    assert activities.forgotten == [graph_run(run)]


async def test_workflow_ends_with_what_the_last_step_stored(temporal, task_queue, some_run):
    async with serving(temporal, task_queue, ScriptedActivities()) as execution:
        assert await execution.finish(some_run()) == STORED


async def test_step_after_the_clone_is_given_what_came_of_the_clone(temporal, task_queue, some_run):
    activities = ScriptedActivities()
    async with serving(temporal, task_queue, activities) as execution:
        await execution.finish(some_run())
    (told,) = activities.analysed
    assert (told.succeeded, told.result) == (True, CLONED)


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


# ------------------------------------------------------------------ the limits
async def test_clone_is_an_activity_of_its_own_under_the_limits_declared_on_the_action(
    temporal, task_queue, some_run
):
    run = some_run()
    async with serving(temporal, task_queue, ScriptedActivities()) as execution:
        await execution.finish(run)
        history = await execution.history(run)
    assert limits_of(history, RUN_ACTION) == Limits(
        start_to_close=timedelta(minutes=15),
        heartbeat=timedelta(seconds=60),
        max_attempts=5,
        first_wait=timedelta(seconds=60),
    )


async def test_steps_of_the_graph_run_under_the_limits_the_worker_configured(
    temporal, task_queue, some_run
):
    configured = AnalysisPolicy(
        attempt_timeout_seconds=47,
        heartbeat_timeout_seconds=11,
        max_attempts=3,
        retry_after_seconds=7,
    )
    run = some_run()
    async with serving(temporal, task_queue, ScriptedActivities(policy=configured)) as execution:
        await execution.finish(run)
        history = await execution.history(run)
    assert limits_of(history, RUN_GRAPH_STEP) == Limits(
        start_to_close=timedelta(seconds=47),
        heartbeat=timedelta(seconds=11),
        max_attempts=3,
        first_wait=timedelta(seconds=7),
    )


# ---------------------------------------------------------- failures worth a retry
async def test_analysis_is_tried_again_after_a_failure_that_may_pass(
    temporal, task_queue, some_run
):
    limited = failure(steps.MODEL_UNAVAILABLE, "HTTP 429: rate limited")
    activities, run = ScriptedActivities(analysis_outcomes=[limited, None]), some_run()
    async with serving(temporal, task_queue, activities) as execution:
        await execution.finish(run)
    assert (len(activities.analysed), len(activities.cloned), activities.recorded) == (2, 1, [])


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


async def test_attempts_used_up_behind_one_still_running_are_told_in_the_analyzers_words(
    temporal, task_queue, some_run
):
    held = ApplicationError(vocabulary.EARLIER_ATTEMPT_RUNNING, type=ATTEMPT_STILL_RUNNING)
    activities = ScriptedActivities(analysis_outcomes=[held, held])
    async with serving(temporal, task_queue, activities) as execution:
        await execution.fail(some_run())
    assert [failed.error for failed in activities.recorded] == [
        "an earlier attempt at the analysis is still running"
    ]


# ------------------------------------------------------ failures not worth a retry
@pytest.mark.parametrize("kind", steps.NOT_RETRIED)
async def test_refused_analysis_is_not_tried_again_and_its_reason_is_recorded(
    temporal, task_queue, some_run, kind
):
    refused = failure(kind, "no profile after 40 steps")
    activities, run = ScriptedActivities(analysis_outcomes=[refused, None]), some_run()
    async with serving(temporal, task_queue, activities) as execution:
        await execution.fail(run)
    assert len(activities.analysed) == 1
    assert activities.recorded == [steps.RunFailure(run=run, error="no profile after 40 steps")]


async def test_refused_repository_is_cloned_once_and_ends_the_run_with_its_reason(
    temporal, task_queue, some_run
):
    refused = failure(steps.ANALYSIS_FAILED, "git clone failed: repository not found")
    activities, run = ScriptedActivities(clone_outcome=refused), some_run()
    async with serving(temporal, task_queue, activities) as execution:
        await execution.fail(run)
    assert len(activities.cloned) == 1
    # The graph is told, and ends the analysis with the words it was told.
    assert [(told.succeeded, told.error) for told in activities.analysed] == [
        (False, "git clone failed: repository not found")
    ]
    assert activities.recorded == [
        steps.RunFailure(run=run, error="git clone failed: repository not found")
    ]


@pytest.mark.parametrize(
    ("of_the_action", "of_the_analysis"),
    [
        ("the action did not finish in time", "the analysis did not finish in time"),
        (
            "the worker running the action stopped responding",
            "the worker running the analysis stopped responding",
        ),
        ("the action stopped unexpectedly", "the analysis stopped unexpectedly"),
    ],
)
async def test_clone_that_ended_without_a_reason_of_its_own_is_told_in_the_analyzers_words(
    temporal, task_queue, some_run, of_the_action, of_the_analysis
):
    # What the graph is told of such a clone is the runtime's sentence, and the analysis ends
    # with it; the clone's own limits are too long to wait for here.
    ended = failure(steps.ANALYSIS_FAILED, of_the_action)
    activities = ScriptedActivities(analysis_outcomes=[ended])
    async with serving(temporal, task_queue, activities) as execution:
        await execution.fail(some_run())
    assert [failed.error for failed in activities.recorded] == [of_the_analysis]


async def test_run_that_cannot_be_started_is_neither_cloned_nor_analysed(
    temporal, task_queue, some_run
):
    finished = failure(steps.RUN_UNAVAILABLE, "analysis run has already failed")
    activities, run = ScriptedActivities(start_outcome=finished), some_run()
    async with serving(temporal, task_queue, activities) as execution:
        await execution.fail(run)
    assert (len(activities.started), activities.cloned, activities.analysed) == (1, [], [])
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
    assert [failed.error for failed in leaking.recorded] == [
        steps.ANALYSIS_TEXTS.stopped_unexpectedly
    ]


# ----------------------------------------------------- timeouts and cancellation
async def test_attempt_that_takes_too_long_is_recorded_as_timed_out(temporal, task_queue, some_run):
    impatient = AnalysisPolicy(
        attempt_timeout_seconds=1, heartbeat_timeout_seconds=10, max_attempts=1
    )
    hanging = ScriptedActivities(policy=impatient, analysis_outcomes=[5])
    async with serving(temporal, task_queue, hanging) as execution:
        await execution.fail(some_run())
    assert [failed.error for failed in hanging.recorded] == [steps.ANALYSIS_TEXTS.timed_out]


async def test_worker_that_stops_sending_heartbeats_is_noticed_before_the_attempt_ends(
    temporal, task_queue, some_run
):
    silent = AnalysisPolicy(attempt_timeout_seconds=60, heartbeat_timeout_seconds=1, max_attempts=1)
    hanging = ScriptedActivities(policy=silent, analysis_outcomes=[5])
    async with serving(temporal, task_queue, hanging) as execution:
        await execution.fail(some_run())
    assert [failed.error for failed in hanging.recorded] == [steps.ANALYSIS_TEXTS.worker_stopped]


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
    assert (len(activities.analysed), activities.recorded) == (2, [])


async def test_cancelled_workflow_still_records_the_failure(temporal, task_queue, some_run):
    activities, run = ScriptedActivities(analysis_outcomes=[30]), some_run()
    async with serving(temporal, task_queue, activities) as execution:
        handle = await execution.start(run)
        await asyncio.wait_for(activities.analysing.wait(), timeout=20)
        await handle.cancel()
        with pytest.raises(WorkflowFailureError) as stopped:
            await handle.result()
    assert isinstance(stopped.value.__cause__, CancelledError)
    assert activities.recorded == [steps.RunFailure(run=run, error=steps.ANALYSIS_TEXTS.cancelled)]


# --------------------------------------------------------------- the forgetting
@pytest.mark.parametrize(
    "ending",
    [
        ScriptedActivities(start_outcome=failure(steps.RUN_UNAVAILABLE, "already failed")),
        ScriptedActivities(clone_outcome=failure(steps.ANALYSIS_FAILED, "not found")),
        ScriptedActivities(analysis_outcomes=[failure(steps.ANALYSIS_FAILED, "no profile")]),
    ],
    ids=["not started", "not cloned", "not analysed"],
)
async def test_run_that_failed_is_forgotten_after_its_failure_is_recorded(
    temporal, task_queue, some_run, ending
):
    run = some_run()
    async with serving(temporal, task_queue, ending) as execution:
        await execution.fail(run)
        history = await execution.history(run)
    assert ending.forgotten == [graph_run(run)]
    assert scheduled(history)[-2:] == [steps.RECORD_FAILURE, FORGET_RUN]


async def test_cancelled_run_is_forgotten(temporal, task_queue, some_run):
    activities, run = ScriptedActivities(analysis_outcomes=[30]), some_run()
    async with serving(temporal, task_queue, activities) as execution:
        handle = await execution.start(run)
        await asyncio.wait_for(activities.analysing.wait(), timeout=20)
        await handle.cancel()
        with pytest.raises(WorkflowFailureError):
            await handle.result()
    assert activities.forgotten == [graph_run(run)]


# ------------------------------------------------------- what the sandbox loads
def test_module_of_the_workflow_loads_no_database_graph_framework_or_activity_code():
    code = (
        "import sys, json; import openmarketer_worker.repository_analyzer.workflow; "
        "print(json.dumps(sorted(sys.modules)))"
    )
    # ``-I``: nothing of this process's environment or working folder reaches the import.
    done = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    forbidden = (
        "langgraph",
        "langchain",
        "sqlalchemy",
        "psycopg",
        "openmarketer_core.db",
        "openmarketer_core.graph_checkpoints",
        "temporalio.activity",
        "openmarketer_worker.repository_analyzer.wiring",
        "openmarketer_worker.repository_analyzer.analysis",
        "openmarketer_worker.repository_analyzer.clones",
    )
    assert [name for name in json.loads(done.stdout) if name.startswith(forbidden)] == []
