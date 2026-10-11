"""Tests for the runtime's three activities as plain functions, against PostgreSQL.

They run in Temporal's activity environment, which needs no server. The workflow side, and how
the activities behave when Temporal retries them, is in ``test_driver.py``.
"""

import asyncio
import logging
import uuid
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import timedelta

import pytest
from scripted_actions import (
    BlockingAction,
    ScriptedAction,
    SendEmail,
    nested,
    routes_of,
)
from scripted_graphs import (
    SEND_THEN_PUBLISH,
    Ask,
    BlockingGraph,
    GraphOfNode,
    RaisingGraph,
    ReturningGraph,
    ScriptedGraph,
    asks_with_an_unregistered_model,
    steps_of_an_agent_whose_route_chooses_outside_its_targets,
)
from scripted_rig import happened, some_run, thread_of
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from openmarketer_core.agent_runtime.actions import ActionOutcome, ActionRequest
from openmarketer_core.agent_runtime.steps import GraphRunRef, MissingInitialState, StepOutcome
from openmarketer_core.db.session import DatabaseError
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_worker.agent_runtime.activities import RuntimeActivities
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess
from openmarketer_worker.agent_runtime.vocabulary import (
    ACTION_TEXTS,
    ATTEMPT_STILL_RUNNING,
    DATABASE_UNAVAILABLE,
    EARLIER_ATTEMPT_RUNNING,
    INVALID_ACTION_PAYLOAD,
    INVALID_ACTION_RESULT,
    MAX_RESULT_BYTES,
    MAX_STEP_BYTES,
    RUN_TEXTS,
    STEP_REFUSED,
    UNEXPECTED,
    UNKNOWN_ACTION,
    StepInput,
)

SECRET = "sk-live-4242-not-for-the-run"


def asking_for(name: str, payload: dict, *, run=None) -> ActionRequest:
    return ActionRequest(
        action_id=f"{thread_of(run or some_run())}:0123456789abcdef", name=name, payload=payload
    )


def two_handlers() -> tuple[ScriptedAction, ScriptedAction]:
    return ScriptedAction(), ScriptedAction()


def with_a_heartbeat_timeout(environment: ActivityEnvironment, *, seconds: float):
    """The activity info of ``environment`` as a workflow that set a heartbeat timeout gives it."""
    return replace(environment.info, heartbeat_timeout=timedelta(seconds=seconds))


async def refused(activity, argument) -> ApplicationError:
    with pytest.raises(ApplicationError) as failure:
        await ActivityEnvironment().run(activity, argument)
    return failure.value


# ------------------------------------------------------------------ run_graph_step
async def test_first_step_starts_the_graph_and_reports_the_action_it_waits_for(runtime):
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH), some_run()
    activities = runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))
    step = await ActivityEnvironment().run(activities.run_graph_step, StepInput(run, None))
    assert step.output is None
    assert step.action is not None
    assert (step.action.name, step.action.payload) == ("send_email", {"to": "ada@example.com"})
    assert step.action.action_id.startswith(f"{thread_of(run)}:")


async def test_step_that_is_asked_again_reports_the_same_action(runtime):
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH), some_run()
    activities = runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))
    first = await ActivityEnvironment().run(activities.run_graph_step, StepInput(run, None))
    again = await ActivityEnvironment().run(activities.run_graph_step, StepInput(run, None))
    assert again == first


async def test_step_with_the_outcome_of_the_action_goes_on_to_the_end(runtime):
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH[:1]), some_run()
    activities = runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))
    asked = await ActivityEnvironment().run(activities.run_graph_step, StepInput(run, None))
    assert asked.action is not None
    outcome = ActionOutcome(asked.action.action_id, True, {"message_id": "m-1"}, "")
    finished = await ActivityEnvironment().run(activities.run_graph_step, StepInput(run, outcome))
    assert finished.action is None
    assert finished.output is not None
    assert finished.output["answers"][0]["result"] == {"message_id": "m-1"}


async def test_step_gives_the_graph_the_thread_of_its_run(runtime, checkpoints):
    seen: list[str] = []

    class Recording(ScriptedGraph):
        def advance(self, run, checkpoints, resume):
            seen.append(checkpoints.thread_id)
            return super().advance(run, checkpoints, resume)

    run = some_run()
    activities = runtime(Recording([]), routes_of(ScriptedAction(), ScriptedAction()))
    await ActivityEnvironment().run(activities.run_graph_step, StepInput(run, None))
    assert seen == [thread_of(run)]


async def test_step_sends_heartbeats_while_the_graph_works_as_the_workflow_asked(runtime):
    environment, heartbeats = ActivityEnvironment(), []
    environment.info = with_a_heartbeat_timeout(environment, seconds=3)
    environment.on_heartbeat = lambda *details: heartbeats.append(details)
    activities = runtime(ScriptedGraph([]), routes_of(ScriptedAction(), ScriptedAction()))
    await environment.run(activities.run_graph_step, StepInput(some_run(), None))
    assert heartbeats


async def test_step_scheduled_without_a_heartbeat_timeout_sends_no_heartbeat(runtime):
    environment, heartbeats = ActivityEnvironment(), []
    environment.on_heartbeat = lambda *details: heartbeats.append(details)
    activities = runtime(ScriptedGraph([]), routes_of(ScriptedAction(), ScriptedAction()))
    await environment.run(activities.run_graph_step, StepInput(some_run(), None))
    assert heartbeats == []


async def test_step_that_is_refused_for_good_says_why_and_is_not_worth_another_attempt(runtime):
    graph = RaisingGraph(MissingInitialState("scripted-agent:1"))
    activities = runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))
    failure = await refused(activities.run_graph_step, StepInput(some_run(), None))
    assert (failure.type, failure.non_retryable) == (STEP_REFUSED, True)
    assert (
        failure.message
        == "thread scripted-agent:1 has no checkpoint and was given no initial state"
    )
    assert failure.__cause__ is None


async def test_outcome_the_thread_never_asked_for_is_refused_for_good(runtime):
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH), some_run()
    activities = runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))
    foreign = ActionOutcome(f"{thread_of(run)}:00000000000000000000000000000000", True, {}, "")
    failure = await refused(activities.run_graph_step, StepInput(run, foreign))
    assert (failure.type, failure.non_retryable) == (STEP_REFUSED, True)


async def test_failure_the_graph_worded_passes_as_it_is(runtime):
    worded = ApplicationError("the repository is empty", type="EmptyRepository", non_retryable=True)
    activities = runtime(RaisingGraph(worded), routes_of(ScriptedAction(), ScriptedAction()))
    failure = await refused(activities.run_graph_step, StepInput(some_run(), None))
    assert (failure.type, failure.message, failure.non_retryable) == (
        "EmptyRepository",
        "the repository is empty",
        True,
    )


async def test_error_nobody_worded_is_reported_without_its_detail_and_tried_again(runtime):
    graph = RaisingGraph(RuntimeError(f"/Users/someone/secret {SECRET}"))
    activities = runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))
    failure = await refused(activities.run_graph_step, StepInput(some_run(), None))
    assert (failure.type, failure.message) == (UNEXPECTED, RUN_TEXTS.stopped_unexpectedly)
    assert not failure.non_retryable
    assert failure.__cause__ is None


async def test_failure_of_the_database_is_reported_as_one_worth_another_attempt(runtime):
    graph = RaisingGraph(DatabaseError("connection is closed"))
    activities = runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))
    failure = await refused(activities.run_graph_step, StepInput(some_run(), None))
    assert (failure.type, failure.message) == (DATABASE_UNAVAILABLE, RUN_TEXTS.database_failed)
    assert not failure.non_retryable


async def test_step_is_refused_while_an_earlier_attempt_holds_the_thread(runtime, checkpoints):
    graph, run = ScriptedGraph([]), some_run()
    activities = runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))
    with checkpoints.of_attempt(thread_of(run)):
        failure = await refused(activities.run_graph_step, StepInput(run, None))
    assert (failure.type, failure.non_retryable) == (ATTEMPT_STILL_RUNNING, False)
    assert failure.message == EARLIER_ATTEMPT_RUNNING
    assert graph.advanced == []


async def test_thread_is_free_again_once_the_step_has_returned(runtime):
    graph, run = ScriptedGraph([]), some_run()
    activities = runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))
    await ActivityEnvironment().run(activities.run_graph_step, StepInput(run, None))
    await ActivityEnvironment().run(activities.run_graph_step, StepInput(run, None))
    assert len(graph.advanced) == 2


async def test_step_that_is_cancelled_while_the_graph_works_says_so_in_the_log(runtime, caplog):
    blocked = BlockingGraph()
    environment = ActivityEnvironment()
    environment.info = with_a_heartbeat_timeout(environment, seconds=3)
    activities = runtime(blocked, routes_of(ScriptedAction(), ScriptedAction()))
    running = asyncio.create_task(
        environment.run(activities.run_graph_step, StepInput(some_run(), None))
    )
    try:
        await happened(blocked.started)
        with caplog.at_level(logging.WARNING):
            environment.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
    finally:
        blocked.release.set()
    assert "was cancelled while its work was running" in caplog.text


async def test_node_that_asks_with_a_model_no_action_was_declared_for_is_refused_for_good(runtime):
    activities = runtime(GraphOfNode(asks_with_an_unregistered_model), routes_of(*two_handlers()))
    failure = await refused(activities.run_graph_step, StepInput(some_run(), None))
    assert (failure.type, failure.non_retryable) == (STEP_REFUSED, True)
    assert failure.message == (
        "model Unregistered is not the payload of any action: "
        "register it with the agent's action decorator"
    )
    assert failure.__cause__ is None


async def test_route_that_chooses_outside_its_targets_is_refused_for_good(runtime):
    graph = steps_of_an_agent_whose_route_chooses_outside_its_targets()
    activities = runtime(graph, routes_of(*two_handlers()))
    run = GraphRunRef(kind="scripted-route", run_id=uuid.uuid4())
    failure = await refused(activities.run_graph_step, StepInput(run, None))
    assert (failure.type, failure.non_retryable) == (STEP_REFUSED, True)
    assert failure.message == (
        "the route after node 'first' chose something that is not one of its targets: second"
    )
    assert "elsewhere" not in failure.message


# ------------------------------------------- what a step may hand over to the history
BULK = "BULK" * (MAX_STEP_BYTES // 4 + 1)


async def test_step_that_asks_with_a_payload_over_the_limit_is_refused_for_good(runtime):
    graph = ScriptedGraph([Ask("send_email", {"to": BULK})])
    activities = runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))
    failure = await refused(activities.run_graph_step, StepInput(some_run(), None))
    assert (failure.type, failure.non_retryable) == (STEP_REFUSED, True)
    assert str(MAX_STEP_BYTES) in failure.message
    assert "BULK" not in failure.message


async def test_step_that_finishes_with_an_output_over_the_limit_is_refused_for_good(runtime):
    activities = runtime(
        ScriptedGraph([], note=BULK), routes_of(ScriptedAction(), ScriptedAction())
    )
    failure = await refused(activities.run_graph_step, StepInput(some_run(), None))
    assert (failure.type, failure.non_retryable) == (STEP_REFUSED, True)
    assert str(MAX_STEP_BYTES) in failure.message
    assert "BULK" not in failure.message


@pytest.mark.parametrize(
    "output",
    [{"x": float("nan")}, {"x": float("inf")}, nested(20000), {"x": {"a", "b"}}],
    ids=["nan", "inf", "deep", "set"],
)
async def test_step_that_finishes_with_an_output_that_is_not_json_is_refused_for_good(
    runtime, output
):
    graph = ReturningGraph(StepOutcome.finished(output))
    activities = runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))
    failure = await refused(activities.run_graph_step, StepInput(some_run(), None))
    assert (failure.type, failure.non_retryable) == (STEP_REFUSED, True)


async def test_step_whose_kind_of_run_cannot_name_a_thread_is_refused_for_good(runtime):
    graph = ScriptedGraph([])
    activities = runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))
    unnamed = GraphRunRef(kind="Not a kind!", run_id=uuid.uuid4())
    failure = await refused(activities.run_graph_step, StepInput(unnamed, None))
    assert (failure.type, failure.non_retryable) == (STEP_REFUSED, True)
    assert graph.advanced == []


# ------------------------------------------------------------------ run_action
async def test_action_runs_its_handler_with_the_payload_as_its_model_and_returns_the_result(
    runtime,
):
    send = ScriptedAction([{"message_id": "m-1"}])
    activities = runtime(ScriptedGraph([]), routes_of(send, ScriptedAction()))
    request = asking_for("send_email", {"to": "ada@example.com"})
    outcome = await ActivityEnvironment().run(activities.run_action, request)
    assert outcome == ActionOutcome(request.action_id, True, {"message_id": "m-1"}, "")
    ((payload, context),) = send.calls
    assert payload == SendEmail(to="ada@example.com")
    assert context.action_id == request.action_id


async def test_action_that_is_not_allowed_runs_nothing_and_is_not_worth_another_attempt(runtime):
    send = ScriptedAction()
    activities = runtime(ScriptedGraph([]), routes_of(send, ScriptedAction()))
    failure = await refused(activities.run_action, asking_for("drop_table", {}))
    assert (failure.type, failure.non_retryable) == (UNKNOWN_ACTION, True)
    assert failure.message == "action 'drop_table' is not one of the allowed actions"
    assert send.calls == []


async def test_payload_that_does_not_fit_runs_nothing_and_is_not_repeated_in_the_failure(runtime):
    send = ScriptedAction()
    activities = runtime(ScriptedGraph([]), routes_of(send, ScriptedAction()))
    request = asking_for("send_email", {"to": "ada", "bcc": SECRET})
    failure = await refused(activities.run_action, request)
    assert (failure.type, failure.non_retryable) == (INVALID_ACTION_PAYLOAD, True)
    assert failure.message == "payload of action 'send_email' does not fit: 'bcc'"
    assert failure.__cause__ is None
    assert SECRET not in str(failure)
    assert send.calls == []


async def test_error_a_handler_raised_is_reported_without_its_detail_and_tried_again(runtime):
    send = ScriptedAction([RuntimeError(f"the service refused the key {SECRET}")])
    activities = runtime(ScriptedGraph([]), routes_of(send, ScriptedAction()))
    failure = await refused(activities.run_action, asking_for("send_email", {"to": "ada"}))
    assert (failure.type, failure.message) == (UNEXPECTED, ACTION_TEXTS.stopped_unexpectedly)
    assert not failure.non_retryable
    assert failure.__cause__ is None


async def test_failure_a_handler_worded_passes_as_it_is(runtime):
    worded = ApplicationError(
        "the mail server refused the address", type="Refused", non_retryable=True
    )
    activities = runtime(ScriptedGraph([]), routes_of(ScriptedAction([worded]), ScriptedAction()))
    failure = await refused(activities.run_action, asking_for("send_email", {"to": "ada"}))
    assert (failure.type, failure.message, failure.non_retryable) == (
        "Refused",
        "the mail server refused the address",
        True,
    )


async def test_failure_of_the_database_in_a_handler_is_worth_another_attempt(runtime):
    send = ScriptedAction([DatabaseError("connection is closed")])
    activities = runtime(ScriptedGraph([]), routes_of(send, ScriptedAction()))
    failure = await refused(activities.run_action, asking_for("send_email", {"to": "ada"}))
    assert (failure.type, failure.message) == (DATABASE_UNAVAILABLE, ACTION_TEXTS.database_failed)
    assert not failure.non_retryable


def a_result_making_an_outcome_of(request: ActionRequest, size: int) -> dict[str, str]:
    """A result that makes the outcome of ``request`` exactly ``size`` bytes, as encoded."""
    encoder = DataConverter.default.payload_converter
    empty = ActionOutcome(request.action_id, succeeded=True, result={"b": ""}, error="")
    return {"b": "x" * (size - len(encoder.to_payload(empty).data))}


async def test_outcome_of_exactly_the_largest_size_is_returned(runtime):
    request = asking_for("send_email", {"to": "ada"})
    send = ScriptedAction([a_result_making_an_outcome_of(request, MAX_RESULT_BYTES)])
    activities = runtime(ScriptedGraph([]), routes_of(send, ScriptedAction()))
    outcome = await ActivityEnvironment().run(activities.run_action, request)
    assert outcome.succeeded


async def test_outcome_one_byte_over_is_refused_for_good_and_does_not_repeat_the_content(runtime):
    request = asking_for("send_email", {"to": "ada"})
    send = ScriptedAction([a_result_making_an_outcome_of(request, MAX_RESULT_BYTES + 1)])
    activities = runtime(ScriptedGraph([]), routes_of(send, ScriptedAction()))
    failure = await refused(activities.run_action, request)
    assert failure.type == "ActionResultTooLarge"
    assert failure.non_retryable
    assert failure.message == f"the result of action send_email is over {MAX_RESULT_BYTES} bytes"
    assert len(send.calls) == 1


async def test_result_that_is_not_json_is_refused_for_good(runtime):
    send = ScriptedAction([{"recipients": {"ada", "grace"}}])
    activities = runtime(ScriptedGraph([]), routes_of(send, ScriptedAction()))
    failure = await refused(activities.run_action, asking_for("send_email", {"to": "ada"}))
    assert (failure.type, failure.non_retryable) == (INVALID_ACTION_RESULT, True)
    assert failure.message == "the result of action send_email is not JSON"


async def test_action_scheduled_without_a_heartbeat_timeout_sends_no_heartbeat(runtime):
    environment, heartbeats = ActivityEnvironment(), []
    environment.on_heartbeat = lambda *details: heartbeats.append(details)
    activities = runtime(ScriptedGraph([]), routes_of(ScriptedAction(), ScriptedAction()))
    await environment.run(activities.run_action, asking_for("send_email", {"to": "ada"}))
    assert heartbeats == []


async def test_action_sends_heartbeats_while_it_runs_as_the_workflow_asked(runtime):
    environment, heartbeats = ActivityEnvironment(), []
    environment.info = with_a_heartbeat_timeout(environment, seconds=3)
    environment.on_heartbeat = lambda *details: heartbeats.append(details)
    activities = runtime(ScriptedGraph([]), routes_of(ScriptedAction(), ScriptedAction()))
    await environment.run(activities.run_action, asking_for("send_email", {"to": "ada"}))
    assert heartbeats


async def test_action_that_is_cancelled_while_it_runs_says_so_in_the_log(runtime, caplog):
    blocked = BlockingAction()
    activities = runtime(ScriptedGraph([]), routes_of(blocked, ScriptedAction()))
    environment = ActivityEnvironment()
    environment.info = with_a_heartbeat_timeout(environment, seconds=3)
    request = asking_for("send_email", {"to": "ada"})
    running = asyncio.create_task(environment.run(activities.run_action, request))
    try:
        await happened(blocked.started)
        with caplog.at_level(logging.WARNING):
            environment.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
    finally:
        blocked.release.set()
    assert f"action {request.action_id} was cancelled" in caplog.text


@pytest.mark.parametrize(
    "result",
    [nested(1000), nested(5000), nested(20000), {"x": float("nan")}, {"x": float("inf")}],
    ids=["depth-1000", "depth-5000", "depth-20000", "nan", "inf"],
)
async def test_result_the_history_cannot_hold_is_refused_for_good_and_the_action_ran_once(
    runtime, result
):
    send = ScriptedAction([result, {"message_id": "m-1"}])
    activities = runtime(ScriptedGraph([]), routes_of(send, ScriptedAction()))
    failure = await refused(activities.run_action, asking_for("send_email", {"to": "ada"}))
    assert (failure.type, failure.non_retryable) == (INVALID_ACTION_RESULT, True)
    assert len(send.calls) == 1


# ------------------------------------------------------------------ forget_run
async def test_forget_run_removes_the_checkpoints_the_run_made(runtime, database):
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH), some_run()
    activities = runtime(graph, routes_of(*two_handlers()))
    await ActivityEnvironment().run(activities.run_graph_step, StepInput(run, None))
    assert database.thread_has_checkpoints(thread_of(run))
    await ActivityEnvironment().run(activities.forget_run, run)
    assert not database.thread_has_checkpoints(thread_of(run))


async def test_forget_run_leaves_the_checkpoints_of_another_run(runtime, database):
    graph, run, other = ScriptedGraph(SEND_THEN_PUBLISH), some_run(), some_run()
    activities = runtime(graph, routes_of(*two_handlers()))
    await ActivityEnvironment().run(activities.run_graph_step, StepInput(run, None))
    await ActivityEnvironment().run(activities.run_graph_step, StepInput(other, None))
    await ActivityEnvironment().run(activities.forget_run, run)
    assert database.thread_has_checkpoints(thread_of(other))


async def test_forget_run_of_a_run_with_no_checkpoints_does_nothing(runtime):
    activities = runtime(ScriptedGraph([]), routes_of(*two_handlers()))
    assert await ActivityEnvironment().run(activities.forget_run, some_run()) is None


async def test_forget_run_forgets_checkpoints_an_attempt_still_holds(
    runtime, checkpoints, database
):
    graph, run = ScriptedGraph(SEND_THEN_PUBLISH), some_run()
    activities = runtime(graph, routes_of(*two_handlers()))
    await ActivityEnvironment().run(activities.run_graph_step, StepInput(run, None))
    with checkpoints.of_attempt(thread_of(run)):
        await ActivityEnvironment().run(activities.forget_run, run)
    assert not database.thread_has_checkpoints(thread_of(run))


def never_attempted(thread_id: str) -> AbstractContextManager[RunCheckpoints]:
    raise AssertionError("forgetting a run takes no attempt at its graph")


def forgetting_fails_with(error: Exception) -> RuntimeActivities:
    def forget(thread_id: str) -> None:
        raise error

    access = CheckpointAccess(of_attempt=never_attempted, forget=forget)
    return RuntimeActivities(access, ScriptedGraph([]), routes_of(*two_handlers()))


async def test_forget_run_does_not_fail_for_a_database_that_is_down(caplog):
    activities = forgetting_fails_with(DatabaseError("connection is closed"))
    with caplog.at_level(logging.WARNING):
        assert await ActivityEnvironment().run(activities.forget_run, some_run()) is None
    assert "were not forgotten, the cleanup removes them" in caplog.text


async def test_forget_run_that_stops_on_an_error_nobody_worded_is_tried_again_without_its_detail():
    activities = forgetting_fails_with(RuntimeError(f"disk path {SECRET}"))
    failure = await refused(activities.forget_run, some_run())
    assert (failure.type, failure.message) == (UNEXPECTED, RUN_TEXTS.stopped_unexpectedly)
    assert not failure.non_retryable
    assert SECRET not in str(failure)


async def test_forget_run_of_a_kind_that_cannot_name_a_thread_is_refused_for_good(runtime):
    activities = runtime(ScriptedGraph([]), routes_of(*two_handlers()))
    unnamed = GraphRunRef(kind="Not a kind!", run_id=uuid.uuid4())
    failure = await refused(activities.forget_run, unnamed)
    assert (failure.type, failure.non_retryable) == (STEP_REFUSED, True)
