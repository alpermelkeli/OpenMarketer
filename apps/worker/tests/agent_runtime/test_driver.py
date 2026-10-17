"""Tests for ``run_graph_with_actions`` and the runtime's activities, together, on a real server.

A scripted graph asks for actions and scripted handlers run them. The workflow is the one of
``scripted_agent.py`` in the workflow sandbox, the activities are the runtime's own, and the
checkpoints are the real ones in PostgreSQL. Timeouts are real, and short. Each test sets up
what it needs through the ``runtime`` and ``serving`` fixtures of ``conftest.py``.
"""

import asyncio
import uuid
from dataclasses import replace
from typing import Any

import pytest
from scripted_actions import (
    BlockingAction,
    ScriptedAction,
    SlowAction,
    WatchingAction,
    routes_of,
)
from scripted_agent import RecordsEveryFailure, RecordsLikeTheAnalyzer, ScriptedAgent
from scripted_graphs import (
    SEND_THEN_PUBLISH,
    Ask,
    BlockingGraph,
    RaisingGraph,
    ScriptedGraph,
    SlowGraph,
)
from scripted_history import exchanges, failures_in
from scripted_rig import (
    ALLOWED,
    REFUSED_BY_A_HANDLER,
    STEP_LIMITS,
    Execution,
    cause_of,
    failed_an_attempt_with,
    happened,
    some_run,
    thread_of,
)
from temporalio.client import WorkflowFailureError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    CancelledError,
    ChildWorkflowError,
    RetryState,
    TimeoutError,
    TimeoutType,
    WorkflowAlreadyStartedError,
)
from temporalio.worker import Worker
from temporalio.workflow import ChildWorkflowCancellationType, ParentClosePolicy

from openmarketer_core.agent_runtime.actions import (
    ActionPolicy,
    ActionRequest,
    RunKind,
    RunTarget,
)
from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.agent_runtime.steps import MissingInitialState, StepOutcome
from openmarketer_worker.agent_runtime import driver, vocabulary
from openmarketer_worker.agent_runtime.vocabulary import (
    ACTION_TEXTS,
    MAX_ERROR_CHARACTERS,
    MAX_RESULT_BYTES,
    WORDED_BY_THE_RUNTIME,
    PreparedRun,
)

# The name of the child the run actions of these tests start, as a target names it.
CHILD_WORKFLOW = "GreetingChild"

SECRET = "sk-live-4242-not-for-history"


# --------------------------------------------------------------------- success
async def test_graph_that_asks_for_two_actions_in_a_row_finishes(runtime, serving):
    send = ScriptedAction([{"message_id": "m-1"}])
    publish = ScriptedAction([{"url": "https://example.com/p/7"}])
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH), some_run()
    async with serving(runtime(graph, routes_of(send, publish))) as execution:
        output = await execution.finish(run)
    assert [answer["result"] for answer in output["answers"]] == [
        {"message_id": "m-1"},
        {"url": "https://example.com/p/7"},
    ]
    assert [answer["succeeded"] for answer in output["answers"]] == [True, True]


async def test_handlers_receive_the_validated_payload_and_the_id_of_their_action(runtime, serving):
    send, publish = ScriptedAction(), ScriptedAction()
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH), some_run()
    async with serving(runtime(graph, routes_of(send, publish))) as execution:
        await execution.finish(run)
    ((sent, _),) = send.calls
    ((published, _),) = publish.calls
    assert (type(sent).__name__, sent.model_dump()) == ("SendEmail", {"to": "ada@example.com"})
    assert (type(published).__name__, published.model_dump()) == ("Publish", {"post": 7})
    assert all(
        action_id.startswith(f"{thread_of(run)}:")
        for action_id in send.action_ids + publish.action_ids
    )
    assert send.action_ids != publish.action_ids


async def test_graph_is_told_what_came_of_each_action_by_the_id_it_asked_with(runtime, serving):
    send, publish = ScriptedAction([{"message_id": "m-1"}]), ScriptedAction([{"post": 7}])
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH), some_run()
    async with serving(runtime(graph, routes_of(send, publish))) as execution:
        output = await execution.finish(run)
    assert output["answers"] == [
        {
            "action_id": send.action_ids[0],
            "succeeded": True,
            "result": {"message_id": "m-1"},
            "error": "",
        },
        {"action_id": publish.action_ids[0], "succeeded": True, "result": {"post": 7}, "error": ""},
    ]
    assert [None if outcome is None else outcome.action_id for outcome in graph.advanced] == [
        None,
        send.action_ids[0],
        publish.action_ids[0],
    ]


async def test_workflow_may_import_the_driver_through_the_sandbox(runtime, serving):
    send, publish = ScriptedAction([{"message_id": "m-1"}]), ScriptedAction()
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH), some_run()
    async with serving(runtime(graph, routes_of(send, publish))) as execution:
        output = await execution.finish_with_the_driver_passed_through(run)
    assert [answer["succeeded"] for answer in output["answers"]] == [True, True]


async def test_graph_that_asks_for_nothing_finishes_after_one_step(runtime, serving):
    send, publish = ScriptedAction(), ScriptedAction()
    graph = ScriptedGraph([])
    async with serving(runtime(graph, routes_of(send, publish))) as execution:
        output = await execution.finish(some_run())
    assert output == {"plan": [], "answers": []}
    assert (send.calls, publish.calls, graph.advanced) == ([], [], [None])


# --------------------------------------------------------------------- retries
async def test_action_tried_again_is_tried_with_the_same_action_id(runtime, serving):
    send = ScriptedAction([RuntimeError("the mail server is down"), {"message_id": "m-1"}])
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH[:1]), some_run()
    async with serving(runtime(graph, routes_of(send, ScriptedAction()))) as execution:
        output = await execution.finish(run)
    first, second = send.action_ids
    assert first == second
    assert output["answers"][0]["result"] == {"message_id": "m-1"}


async def test_action_that_fails_until_its_attempts_are_used_up_is_told_to_the_graph_as_failed(
    runtime, serving
):
    send = ScriptedAction([RuntimeError(f"smtp password {SECRET}")] * 2)
    publish = ScriptedAction()
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH), some_run()
    async with serving(runtime(graph, routes_of(send, publish))) as execution:
        output = await execution.finish(run)
    assert len(send.calls) == ALLOWED["send_email"].max_attempts
    assert output["answers"] == [
        {
            "action_id": send.action_ids[0],
            "succeeded": False,
            "result": {},
            "error": ACTION_TEXTS.stopped_unexpectedly,
        }
    ]
    # The graph decided to stop at the failed action: the second was never asked for.
    assert publish.calls == []


async def test_action_that_fails_for_good_is_told_to_the_graph_with_the_handlers_own_words(
    runtime, serving
):
    refused = ApplicationError(
        "the mail server refused the address", type=REFUSED_BY_A_HANDLER, non_retryable=True
    )
    send = ScriptedAction([refused, {"message_id": "never"}])
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH[:1]), some_run()
    async with serving(runtime(graph, routes_of(send, ScriptedAction()))) as execution:
        output = await execution.finish(run)
    assert len(send.calls) == 1
    assert output["answers"][0]["succeeded"] is False
    assert output["answers"][0]["error"] == "the mail server refused the address"


async def test_failure_of_a_type_the_agent_did_not_declare_is_told_to_the_graph_as_unexpected(
    runtime, serving
):
    refused = ApplicationError(
        "the mail server refused the address", type=REFUSED_BY_A_HANDLER, non_retryable=True
    )
    send = ScriptedAction([refused])
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH[:1]), some_run()
    runtimes_own_types = list(WORDED_BY_THE_RUNTIME)
    async with serving(runtime(graph, routes_of(send, ScriptedAction()))) as execution:
        output = await execution.finish(run, worded_types=runtimes_own_types)
    assert output["answers"][0]["error"] == ACTION_TEXTS.stopped_unexpectedly


async def test_failure_the_workers_library_made_is_told_to_the_graph_as_unexpected(
    runtime, temporal, task_queue
):
    # The worker serves the steps and not the action: the library fails the action itself, with
    # a message that lists every activity the worker has.
    activities = runtime(
        ScriptedGraph(SEND_THEN_PUBLISH[:1]), routes_of(ScriptedAction(), ScriptedAction())
    )
    worker = Worker(
        temporal,
        task_queue=task_queue,
        workflows=[ScriptedAgent],
        activities=[activities.run_graph_step],
    )
    async with worker:
        output = await Execution(temporal, task_queue).finish(some_run())
    assert output["answers"][0]["error"] == ACTION_TEXTS.stopped_unexpectedly
    assert "available activities" not in output["answers"][0]["error"]


async def test_action_that_does_not_finish_in_time_is_told_to_the_graph_as_timed_out(
    runtime, serving
):
    impatient = {"send_email": ActionPolicy(1, 1, 0.1), "publish": ALLOWED["publish"]}
    blocked = BlockingAction()
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH[:1]), some_run()
    try:
        async with serving(runtime(graph, routes_of(blocked, ScriptedAction()))) as execution:
            output = await execution.finish(run, actions=impatient)
    finally:
        blocked.release.set()
    assert output["answers"][0]["error"] == ACTION_TEXTS.timed_out


async def test_step_tried_again_after_it_applied_an_outcome_does_not_run_the_action_again(
    runtime, serving
):
    send, publish = ScriptedAction([{"message_id": "m-1"}]), ScriptedAction()
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH, dies_after_applying=1), some_run()
    async with serving(runtime(graph, routes_of(send, publish))) as execution:
        output = await execution.finish(run)
    assert (len(send.calls), len(publish.calls)) == (1, 1)
    assert [answer["succeeded"] for answer in output["answers"]] == [True, True]
    # The step that applied the outcome died, and its retry was given the same outcome.
    assert graph.advanced[1] == graph.advanced[2]


async def test_step_is_refused_and_tried_again_while_an_attempt_holds_the_thread(
    runtime, serving, checkpoints
):
    graph, run = ScriptedGraph([]), some_run()
    async with serving(runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))) as execution:
        with checkpoints.of_attempt(thread_of(run)):
            failure = await execution.fail(run)
    assert isinstance(failure, ActivityError)
    # Tried again until the attempts of the policy were used up, and then no more.
    assert failure.retry_state is RetryState.MAXIMUM_ATTEMPTS_REACHED
    assert cause_of(failure).type == vocabulary.ATTEMPT_STILL_RUNNING
    assert graph.advanced == []


async def test_step_goes_on_once_the_attempt_that_held_the_thread_has_returned(
    runtime, serving, checkpoints
):
    patient = replace(STEP_LIMITS, max_attempts=10)
    graph, run = ScriptedGraph([]), some_run()
    async with serving(runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))) as execution:
        with checkpoints.of_attempt(thread_of(run)):
            handle = await execution.start(run, policy=patient)
            await failed_an_attempt_with(handle, vocabulary.ATTEMPT_STILL_RUNNING)
            assert graph.advanced == []
        output = await handle.result()
    assert output == {"plan": [], "answers": []}


# --------------------------------------------------------------------- heartbeats
async def test_action_longer_than_the_heartbeat_timeout_of_the_workflow_goes_on_heartbeating(
    runtime, serving
):
    watched = {"send_email": ActionPolicy(30, 1, 0.2, heartbeat_timeout_seconds=2)}
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH[:1]), some_run()
    async with serving(runtime(graph, routes_of(SlowAction(4), ScriptedAction()))) as execution:
        output = await execution.finish(run, actions=watched)
    assert output["answers"][0]["succeeded"] is True


async def test_step_longer_than_the_heartbeat_timeout_of_the_workflow_goes_on_heartbeating(
    runtime, serving
):
    watched = replace(STEP_LIMITS, heartbeat_timeout_seconds=2, max_attempts=1)
    activities = runtime(SlowGraph(4), routes_of(ScriptedAction(), ScriptedAction()))
    async with serving(activities) as execution:
        output = await execution.finish(some_run(), policy=watched)
    assert output == {}


# --------------------------------------------------------------------- what the graph is told
async def test_error_told_to_the_graph_is_cut_to_its_limit(runtime, serving):
    long_reply = ApplicationError(
        "the service said: " + "x" * 5000, type=REFUSED_BY_A_HANDLER, non_retryable=True
    )
    send = ScriptedAction([long_reply])
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH[:1]), some_run()
    async with serving(runtime(graph, routes_of(send, ScriptedAction()))) as execution:
        output = await execution.finish(run)
    error = output["answers"][0]["error"]
    assert len(error) == MAX_ERROR_CHARACTERS
    assert error.startswith("the service said: xxx")
    assert error.endswith("...")


async def test_node_that_asks_for_two_actions_ends_the_run_and_the_second_never_runs(
    runtime, serving
):
    send, publish = ScriptedAction(), ScriptedAction()
    graph, run = ScriptedGraph([Ask("send_email", {})], asking_twice=True), some_run()
    async with serving(runtime(graph, routes_of(send, publish))) as execution:
        failure = await execution.fail(run)
    assert isinstance(failure, ActivityError)
    assert failure.retry_state is RetryState.NON_RETRYABLE_FAILURE
    assert cause_of(failure).type == vocabulary.STEP_REFUSED
    assert (len(send.calls), publish.calls) == (1, [])


# --------------------------------------------------------------------- what is refused
async def test_action_the_workflow_does_not_list_is_refused_and_nothing_runs(runtime, serving):
    send, publish = ScriptedAction(), ScriptedAction()
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH[1:]), some_run()
    only_email = {"send_email": ALLOWED["send_email"]}
    async with serving(runtime(graph, routes_of(send, publish))) as execution:
        failure = await execution.fail(run, actions=only_email)
        history = await execution.history(run)
    assert isinstance(failure, ApplicationError)
    assert (failure.type, failure.non_retryable) == (vocabulary.UNKNOWN_ACTION, True)
    assert "'publish'" in failure.message
    assert publish.calls == []
    assert graph.advanced == [None]
    assert [exchange.activity for exchange in exchanges(history)] == ["run_graph_step"]


async def test_name_of_an_action_the_workflow_does_not_list_is_quoted_short_in_the_failure(
    runtime, serving
):
    long_name = "drop_table\n" + "x" * 500
    graph, run = ScriptedGraph([Ask(long_name, {})]), some_run()
    async with serving(runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))) as execution:
        failure = await execution.fail(run)
    assert isinstance(failure, ApplicationError)
    assert "\n" not in failure.message
    assert len(failure.message) < 150


async def test_payload_that_does_not_fit_ends_the_run_and_the_handler_never_runs(runtime, serving):
    secret_value = "bcc-value-not-to-be-repeated"
    send = ScriptedAction()
    graph, run = ScriptedGraph([Ask("send_email", {"to": "ada", "bcc": secret_value})]), some_run()
    async with serving(runtime(graph, routes_of(send, ScriptedAction()))) as execution:
        failure = await execution.fail(run)
        history = await execution.history(run)
    refusal = cause_of(failure)
    assert (refusal.type, refusal.non_retryable) == (vocabulary.INVALID_ACTION_PAYLOAD, True)
    assert "'<field>'" in refusal.message
    assert send.calls == []
    assert graph.advanced == [None]
    # The action was scheduled once and not tried again; the failure does not repeat the value.
    assert [exchange.activity for exchange in exchanges(history)] == [
        "run_graph_step",
        "run_action",
    ]
    assert isinstance(failure, ActivityError)
    assert failure.retry_state is RetryState.NON_RETRYABLE_FAILURE
    assert secret_value not in failures_in(history)
    assert secret_value not in refusal.message


async def test_action_asked_for_beyond_the_limit_ends_the_run(runtime, serving):
    send, publish = ScriptedAction(), ScriptedAction()
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH), some_run()
    one_action = replace(STEP_LIMITS, max_actions=1)
    async with serving(runtime(graph, routes_of(send, publish))) as execution:
        failure = await execution.fail(run, policy=one_action)
    assert isinstance(failure, ApplicationError)
    assert (failure.type, failure.non_retryable) == (vocabulary.TOO_MANY_ACTIONS, True)
    assert (len(send.calls), publish.calls) == (1, [])


async def test_result_over_the_limit_ends_the_run_and_the_action_is_not_run_again(runtime, serving):
    send = ScriptedAction([{"blob": "x" * MAX_RESULT_BYTES}, {"message_id": "m-1"}])
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH[:1]), some_run()
    async with serving(runtime(graph, routes_of(send, ScriptedAction()))) as execution:
        failure = await execution.fail(run)
    refusal = cause_of(failure)
    assert (refusal.type, refusal.non_retryable) == (vocabulary.ACTION_RESULT_TOO_LARGE, True)
    assert len(send.calls) == 1
    assert isinstance(failure, ActivityError)
    assert failure.retry_state is RetryState.NON_RETRYABLE_FAILURE


# ------------------------------------------- how a workflow can record a refusal of the driver
async def test_workflow_that_catches_what_the_analyzers_does_does_not_catch_a_refusal(
    runtime, serving
):
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH[1:]), some_run()
    only_email = {"send_email": ALLOWED["send_email"]}
    async with serving(runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))) as execution:
        handle = await execution.start(
            run, actions=only_email, workflow_run=RecordsLikeTheAnalyzer.run
        )
        with pytest.raises(WorkflowFailureError) as failed:
            await handle.result()
    # The refusal went by: a workflow that records how its run ended would have recorded nothing.
    refusal = failed.value.__cause__
    assert isinstance(refusal, ApplicationError)
    assert refusal.type == vocabulary.UNKNOWN_ACTION


async def test_workflow_that_catches_application_errors_too_records_a_refusal(runtime, serving):
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH[1:]), some_run()
    only_email = {"send_email": ALLOWED["send_email"]}
    async with serving(runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))) as execution:
        handle = await execution.start(
            run, actions=only_email, workflow_run=RecordsEveryFailure.run
        )
        reason = await handle.result()
    assert reason == "action 'publish' is not one of the allowed actions"


async def test_workflow_that_catches_application_errors_too_records_a_failed_step(runtime, serving):
    graph, run = RaisingGraph(MissingInitialState("scripted-agent:1")), some_run()
    async with serving(runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))) as execution:
        handle = await execution.start(run, workflow_run=RecordsEveryFailure.run)
        reason = await handle.result()
    assert reason == str(MissingInitialState("scripted-agent:1"))


# --------------------------------------------------------------------- failing steps
async def test_step_the_thread_refuses_ends_the_run_at_the_first_attempt(runtime, serving):
    graph = RaisingGraph(MissingInitialState("scripted-agent:1"))
    async with serving(runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))) as execution:
        failure = await execution.fail(some_run())
    refusal = cause_of(failure)
    assert (refusal.type, refusal.non_retryable) == (vocabulary.STEP_REFUSED, True)
    assert "scripted-agent:1" in refusal.message
    assert graph.advanced == 1


async def test_failure_the_graph_worded_reaches_the_workflow_as_it_is(runtime, serving):
    cloning_refused = ApplicationError(
        "the repository cannot be cloned", type="CloneRefused", non_retryable=True
    )
    graph = RaisingGraph(cloning_refused)
    async with serving(runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))) as execution:
        failure = await execution.fail(some_run())
    refusal = cause_of(failure)
    assert (refusal.type, refusal.message) == ("CloneRefused", "the repository cannot be cloned")
    assert graph.advanced == 1


async def test_step_that_takes_too_long_ends_the_run_as_timed_out(runtime, serving):
    impatient = replace(STEP_LIMITS, attempt_timeout_seconds=1, max_attempts=1)
    blocked = BlockingGraph()
    try:
        async with serving(runtime(blocked, routes_of(ScriptedAction(), ScriptedAction()))) as run:
            failure = await run.fail(some_run(), policy=impatient)
    finally:
        blocked.release.set()
    assert isinstance(failure, ActivityError)
    assert isinstance(failure.cause, TimeoutError)
    assert failure.cause.type is TimeoutType.START_TO_CLOSE


# --------------------------------------------------------------------- cancellation
async def test_cancelling_the_workflow_while_an_action_runs_ends_it_and_the_action_hears_of_it(
    runtime, serving
):
    watched = {"send_email": ActionPolicy(30, 2, 0.2, heartbeat_timeout_seconds=3)}
    watching = WatchingAction()
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH[:1]), some_run()
    async with serving(runtime(graph, routes_of(watching, ScriptedAction()))) as execution:
        handle = await execution.start(run, actions=watched)
        await happened(watching.started)
        await handle.cancel()
        with pytest.raises(WorkflowFailureError) as stopped:
            await asyncio.wait_for(handle.result(), timeout=20)
        # The workflow does not wait for the action; the action learns at its next heartbeat.
        await happened(watching.saw_cancellation)
    # A heartbeat told it, as the workflow was closed and its activity no longer exists; an
    # action that sent none would have found out only when the heartbeat timeout ran out.
    assert watching.why is not None
    assert (watching.why.not_found, watching.why.timed_out) == (True, False)
    assert isinstance(stopped.value.__cause__, CancelledError)
    assert graph.advanced == [None]


# ------------------------------------------------------ a step the driver cannot read
async def test_step_that_holds_neither_an_output_nor_an_action_ends_the_run_for_good(monkeypatch):
    # ``StepOutcome`` cannot be built so; a failed assertion in a workflow would fail the workflow
    # task, which is tried again for ever, so the driver refuses it as a failure of the run.
    neither = object.__new__(StepOutcome)
    object.__setattr__(neither, "output", None)
    object.__setattr__(neither, "action", None)

    async def take_step(step, policy):
        return neither

    monkeypatch.setattr(driver, "_take_step", take_step)
    with pytest.raises(ApplicationError) as refused:
        await driver.run_graph_with_actions(some_run(), actions=ALLOWED, policy=STEP_LIMITS)
    assert (refused.value.type, refused.value.non_retryable) == (vocabulary.STEP_REFUSED, True)


# ------------------------------------------------------- actions that start another run
SCOPE = RunScope(workspace_id=uuid.uuid4(), project_id=uuid.uuid4())


def a_target(kind: RunKind) -> RunTarget:
    return RunTarget(
        workflow=CHILD_WORKFLOW,
        task_queue="children-of-the-tests",
        kind=kind,
        longest_seconds=60,
        input_of=lambda child: {"run_id": str(child.run_id), "name": child.payload["name"]},
    )


def a_run_action(name: str = "research") -> ActionRequest:
    return ActionRequest(action_id=f"{thread_of(some_run())}:7", name=name, payload={"name": "Ada"})


class FakeChildren:
    """The child workflow functions of one test: what was started, and what the child answers.

    Replacing ``driver.workflow`` with it: the driver reaches no other function of the engine
    on this path.
    """

    def __init__(
        self,
        *,
        result: Any = None,
        failure: BaseException | None = None,
        start_fails: BaseException | None = None,
        cancellation: str | None = None,
    ) -> None:
        self.result = {"greeting": "hello, Ada"} if result is None else result
        self.failure = failure
        self.start_fails = start_fails
        self.cancellation = cancellation
        self.started: list[dict[str, Any]] = []
        self.awaited: list[dict[str, Any]] = []

    def cancellation_reason(self) -> str | None:
        return self.cancellation

    async def start_child_workflow(self, workflow_name, argument, **limits):
        if self.start_fails is not None:
            raise self.start_fails
        self.started.append({"workflow": workflow_name, "argument": argument, **limits})

    async def execute_child_workflow(self, workflow_name, argument, **limits):
        self.awaited.append({"workflow": workflow_name, "argument": argument, **limits})
        if self.start_fails is not None:
            # An id that is taken fails the start, whether the caller then waits or not.
            raise self.start_fails
        if self.failure is not None:
            raise self.failure
        return self.result


def a_child_failure(message: str) -> ChildWorkflowError:
    return ChildWorkflowError(
        message,
        namespace="default",
        workflow_id="child-1",
        run_id="run-1",
        workflow_type=CHILD_WORKFLOW,
        initiated_event_id=1,
        started_event_id=2,
        retry_state=None,
    )


def a_failed_child() -> ChildWorkflowError:
    return a_child_failure("the child's own words stay in its own history")


def a_cancelled_child() -> ChildWorkflowError:
    failure = a_child_failure("child workflow cancelled")
    failure.__cause__ = CancelledError("the child was cancelled")
    return failure


def preparing(monkeypatch, run_id: uuid.UUID) -> None:
    """Replace the record-creating activity of the driver with one that makes ``run_id``."""

    async def database_write(step, argument, limits, *, not_retried, result_type=None):
        return PreparedRun(run_id=run_id)

    monkeypatch.setattr(driver, "database_write", database_write)


def test_child_workflow_id_is_derived_from_the_run_and_the_action_and_from_nothing_else():
    run = some_run()
    first = driver.child_workflow_id(run, "a:1:2")
    assert first == driver.child_workflow_id(run, "a:1:2")
    assert first != driver.child_workflow_id(run, "a:1:3")
    assert first != driver.child_workflow_id(some_run(), "a:1:2")
    assert "a:1:2" in first


async def test_a_run_waited_for_hands_the_child_its_input_and_gives_its_answer_back(
    monkeypatch,
):
    run, run_id = some_run(), uuid.uuid4()
    children = FakeChildren(result={"greeting": "hello, Ada", "child_run_id": "x"})
    monkeypatch.setattr(driver, "workflow", children)
    preparing(monkeypatch, run_id)
    request = a_run_action()

    outcome = await driver._start_or_wait_the_run(
        run, SCOPE, request, a_target(RunKind.WAIT), WORDED_BY_THE_RUNTIME
    )

    assert outcome.succeeded is True
    assert outcome.result == children.result
    (awaited,) = children.awaited
    assert awaited["workflow"] == CHILD_WORKFLOW
    assert awaited["task_queue"] == "children-of-the-tests"
    assert awaited["id"] == driver.child_workflow_id(run, request.action_id)
    assert awaited["argument"] == {"run_id": str(run_id), "name": "Ada"}
    assert awaited["id_reuse_policy"] is WorkflowIDReusePolicy.REJECT_DUPLICATE
    assert awaited["cancellation_type"] is ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED
    assert awaited["parent_close_policy"] is ParentClosePolicy.ABANDON


async def test_a_run_that_is_started_is_answered_with_its_id_without_waiting(monkeypatch):
    run_id = uuid.uuid4()
    children = FakeChildren()
    monkeypatch.setattr(driver, "workflow", children)
    preparing(monkeypatch, run_id)
    request = a_run_action()

    outcome = await driver._start_or_wait_the_run(
        some_run(), SCOPE, request, a_target(RunKind.START), WORDED_BY_THE_RUNTIME
    )

    assert (outcome.succeeded, outcome.result) == (True, {"run_id": str(run_id)})
    assert children.awaited == []
    (started,) = children.started
    assert started["argument"] == {"run_id": str(run_id), "name": "Ada"}
    assert started["cancellation_type"] is ChildWorkflowCancellationType.ABANDON
    assert started["parent_close_policy"] is ParentClosePolicy.ABANDON


async def test_a_child_that_did_not_finish_is_a_failed_outcome_in_fixed_words(monkeypatch):
    children = FakeChildren(failure=a_failed_child())
    monkeypatch.setattr(driver, "workflow", children)
    preparing(monkeypatch, uuid.uuid4())

    outcome = await driver._start_or_wait_the_run(
        some_run(), SCOPE, a_run_action(), a_target(RunKind.WAIT), WORDED_BY_THE_RUNTIME
    )

    assert outcome.succeeded is False
    assert outcome.error == vocabulary.CHILD_RUN_DID_NOT_FINISH


async def test_a_child_cancelled_for_its_own_sake_is_a_failed_outcome(monkeypatch):
    children = FakeChildren(failure=a_cancelled_child(), cancellation=None)
    monkeypatch.setattr(driver, "workflow", children)
    preparing(monkeypatch, uuid.uuid4())

    outcome = await driver._start_or_wait_the_run(
        some_run(), SCOPE, a_run_action(), a_target(RunKind.WAIT), WORDED_BY_THE_RUNTIME
    )

    assert outcome.succeeded is False
    assert outcome.error == vocabulary.CHILD_RUN_DID_NOT_FINISH


async def test_a_child_cancelled_with_a_cancelled_parent_cancels_the_run(monkeypatch):
    children = FakeChildren(failure=a_cancelled_child(), cancellation="a person cancelled it")
    monkeypatch.setattr(driver, "workflow", children)
    preparing(monkeypatch, uuid.uuid4())

    with pytest.raises(asyncio.CancelledError):
        await driver._start_or_wait_the_run(
            some_run(), SCOPE, a_run_action(), a_target(RunKind.WAIT), WORDED_BY_THE_RUNTIME
        )


async def test_a_child_whose_id_is_taken_is_known_to_a_waiting_action(monkeypatch):
    children = FakeChildren(start_fails=WorkflowAlreadyStartedError("child-1", CHILD_WORKFLOW))
    monkeypatch.setattr(driver, "workflow", children)
    preparing(monkeypatch, uuid.uuid4())

    outcome = await driver._start_or_wait_the_run(
        some_run(), SCOPE, a_run_action(), a_target(RunKind.WAIT), WORDED_BY_THE_RUNTIME
    )

    assert outcome.succeeded is False
    assert outcome.error == vocabulary.CHILD_RUN_ALREADY_STARTED


async def test_a_started_run_whose_id_is_taken_counts_as_started(monkeypatch):
    children = FakeChildren(start_fails=WorkflowAlreadyStartedError("child-1", CHILD_WORKFLOW))
    monkeypatch.setattr(driver, "workflow", children)
    run_id = uuid.uuid4()
    preparing(monkeypatch, run_id)

    outcome = await driver._start_or_wait_the_run(
        some_run(), SCOPE, a_run_action(), a_target(RunKind.START), WORDED_BY_THE_RUNTIME
    )

    assert (outcome.succeeded, outcome.result) == (True, {"run_id": str(run_id)})


async def test_a_child_that_reported_no_small_outcome_is_a_failed_outcome(monkeypatch):
    children = FakeChildren(result="a sentence the child should not have answered with")
    monkeypatch.setattr(driver, "workflow", children)
    preparing(monkeypatch, uuid.uuid4())

    outcome = await driver._start_or_wait_the_run(
        some_run(), SCOPE, a_run_action(), a_target(RunKind.WAIT), WORDED_BY_THE_RUNTIME
    )

    assert outcome.succeeded is False
    assert outcome.error == vocabulary.CHILD_RUN_UNREADABLE


async def test_a_run_action_without_a_scope_is_refused_for_good(monkeypatch):
    request = a_run_action()

    async def take_step(step, policy):
        return StepOutcome.waiting_for(request)

    monkeypatch.setattr(driver, "_take_step", take_step)
    with pytest.raises(ApplicationError) as refused:
        await driver.run_graph_with_actions(
            some_run(),
            actions={"research": ALLOWED["send_email"]},
            policy=STEP_LIMITS,
            run_targets={"research": a_target(RunKind.WAIT)},
            scope=None,
        )
    assert (refused.value.type, refused.value.non_retryable) == (vocabulary.RUN_REFUSED, True)
