"""Tests for the runtime's two activities as plain functions, against PostgreSQL.

They run in Temporal's activity environment, which needs no server. The workflow side, and how
the activities behave when Temporal retries them, is in ``test_driver.py``.
"""

import asyncio
import logging

import pytest
from scripted_work import (
    SEND_THEN_PUBLISH,
    BlockingAction,
    BlockingGraph,
    RaisingGraph,
    ScriptedAction,
    ScriptedGraph,
    SendEmail,
    happened,
    route,
    routes_of,
    some_run,
    thread_of,
)
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from openmarketer_core.agent_runtime.actions import ActionOutcome, ActionRequest
from openmarketer_core.agent_runtime.steps import MissingInitialState
from openmarketer_core.db.session import DatabaseError
from openmarketer_worker.agent_runtime.routes import ActionRoutes
from openmarketer_worker.agent_runtime.vocabulary import (
    ACTION_TEXTS,
    ATTEMPT_STILL_RUNNING,
    DATABASE_UNAVAILABLE,
    EARLIER_ATTEMPT_RUNNING,
    INVALID_ACTION_PAYLOAD,
    INVALID_ACTION_RESULT,
    MAX_RESULT_BYTES,
    RUN_TEXTS,
    STEP_REFUSED,
    UNEXPECTED,
    UNKNOWN_ACTION,
    ActionPolicy,
    StepInput,
)

SECRET = "sk-live-4242-not-for-the-run"


def asking_for(name: str, payload: dict, *, run=None) -> ActionRequest:
    return ActionRequest(
        action_id=f"{thread_of(run or some_run())}:0123456789abcdef", name=name, payload=payload
    )


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


async def test_step_gives_the_graph_the_thread_of_its_run(runtime, checkpoint_access):
    seen: list[str] = []

    class Recording(ScriptedGraph):
        def advance(self, run, checkpoints, resume):
            seen.append(checkpoints.thread_id)
            return super().advance(run, checkpoints, resume)

    run = some_run()
    activities = runtime(Recording([]), routes_of(ScriptedAction(), ScriptedAction()))
    await ActivityEnvironment().run(activities.run_graph_step, StepInput(run, None))
    assert seen == [thread_of(run)]


async def test_step_sends_a_heartbeat_while_the_graph_works(runtime):
    environment = ActivityEnvironment()
    heartbeats: list[object] = []
    environment.on_heartbeat = lambda *details: heartbeats.append(details)
    activities = runtime(ScriptedGraph([]), routes_of(ScriptedAction(), ScriptedAction()))
    await environment.run(activities.run_graph_step, StepInput(some_run(), None))
    assert heartbeats


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
    assert failure is worded


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


async def test_step_is_refused_while_an_earlier_attempt_holds_the_thread(
    runtime, checkpoint_access, caplog
):
    graph, run = ScriptedGraph([]), some_run()
    activities = runtime(graph, routes_of(ScriptedAction(), ScriptedAction()))
    with checkpoint_access.of_attempt(thread_of(run)):
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
    assert failure is worded


async def test_failure_of_the_database_in_a_handler_is_worth_another_attempt(runtime):
    send = ScriptedAction([DatabaseError("connection is closed")])
    activities = runtime(ScriptedGraph([]), routes_of(send, ScriptedAction()))
    failure = await refused(activities.run_action, asking_for("send_email", {"to": "ada"}))
    assert (failure.type, failure.message) == (DATABASE_UNAVAILABLE, ACTION_TEXTS.database_failed)
    assert not failure.non_retryable


def a_result_of(json_bytes: int) -> dict[str, str]:
    """A result whose JSON is exactly ``json_bytes`` long (``{"b":""}`` is 8 of them)."""
    return {"b": "x" * (json_bytes - 8)}


async def test_result_of_exactly_the_largest_size_is_returned(runtime):
    send = ScriptedAction([a_result_of(MAX_RESULT_BYTES)])
    activities = runtime(ScriptedGraph([]), routes_of(send, ScriptedAction()))
    outcome = await ActivityEnvironment().run(
        activities.run_action, asking_for("send_email", {"to": "ada"})
    )
    assert outcome.succeeded


async def test_result_one_byte_over_is_refused_for_good_and_does_not_repeat_the_content(runtime):
    send = ScriptedAction([a_result_of(MAX_RESULT_BYTES + 1)])
    activities = runtime(ScriptedGraph([]), routes_of(send, ScriptedAction()))
    failure = await refused(activities.run_action, asking_for("send_email", {"to": "ada"}))
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


async def test_action_without_a_heartbeat_timeout_sends_no_heartbeat(runtime):
    environment = ActivityEnvironment()
    heartbeats: list[object] = []
    environment.on_heartbeat = lambda *details: heartbeats.append(details)
    activities = runtime(ScriptedGraph([]), routes_of(ScriptedAction(), ScriptedAction()))
    await environment.run(activities.run_action, asking_for("send_email", {"to": "ada"}))
    assert heartbeats == []


async def test_action_with_a_heartbeat_timeout_sends_heartbeats_while_it_runs(runtime):
    environment = ActivityEnvironment()
    heartbeats: list[object] = []
    environment.on_heartbeat = lambda *details: heartbeats.append(details)
    watched = ActionPolicy(30, 2, 0.2, heartbeat_timeout_seconds=3)
    routes = ActionRoutes([route("send_email", SendEmail, ScriptedAction(), watched)])
    activities = runtime(ScriptedGraph([]), routes)
    await environment.run(activities.run_action, asking_for("send_email", {"to": "ada"}))
    assert heartbeats


async def test_action_that_is_cancelled_while_it_runs_says_so_in_the_log(runtime, caplog):
    blocked = BlockingAction()
    watched = ActionPolicy(30, 2, 0.2, heartbeat_timeout_seconds=3)
    routes = ActionRoutes([route("send_email", SendEmail, blocked, watched)])
    activities = runtime(ScriptedGraph([]), routes)
    environment = ActivityEnvironment()
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
