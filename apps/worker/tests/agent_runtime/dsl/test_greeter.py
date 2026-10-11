"""An agent written only with the decorators, on a real Temporal server and real checkpoints.

The greeter (``definitions.py``, ``graph.py``, ``handlers.py``, ``workflow.py``) asks for one
action and reports what it was told. Each test starts its workflow by name, the way the API
does, on a worker serving the agent's registration (``conftest.py``).
"""

import asyncio
from datetime import timedelta

import pytest
from scripted_history import ScheduledWith, exchanges, scheduled_with
from scripted_rig import happened
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError
from temporalio.worker import Replayer

from dsl import graph, handlers
from dsl.definitions import KIND, Greet, Note
from dsl.rig import Starter, some_run, thread_of
from dsl.workflow import Greeter
from dsl.workflow_passthrough import GreeterPassedThrough
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_worker.agent_runtime import vocabulary

WORKFLOWS = {"Greeter": Greeter, "GreeterPassedThrough": GreeterPassedThrough}
EITHER_WORKFLOW = pytest.mark.parametrize("name", list(WORKFLOWS))

HELLO = {"summary": "hello, Ada!"}
NOT_DECLARED = "action 'unlisted' is not one of the allowed actions"


async def a_cancelled_run(starter: Starter, run: GraphRunRef) -> None:
    """Cancels a run of the greeter while its action works, and returns once it has ended."""
    handlers.service.holds = True
    handle = await starter.start("Greeter", run)
    try:
        await happened(handlers.service.started)
        await handle.cancel()
        with pytest.raises(WorkflowFailureError) as stopped:
            await asyncio.wait_for(handle.result(), timeout=20)
    finally:
        handlers.service.release.set()
    assert isinstance(stopped.value.__cause__, CancelledError)


# ------------------------------------------------------------------- the run
async def test_run_pauses_at_the_action_and_finishes_with_what_the_agent_reports(serving):
    async with serving() as starter:
        output = await starter.finish("Greeter", some_run())
    # The result projection, and not the graph's state, which held the name and the reply.
    assert output == HELLO


async def test_handler_gets_the_validated_payload_and_the_id_of_its_action(serving):
    run = some_run()
    graph.subjects[run.run_id] = "Grace"
    async with serving() as starter:
        await starter.finish("Greeter", run)
    ((payload, key),) = handlers.service.calls
    assert payload == Greet(name="Grace")
    assert key.startswith(f"{thread_of(run)}:")


async def test_workflow_with_no_hooks_and_the_agent_passed_through_the_sandbox_runs_it(serving):
    async with serving() as starter:
        output = await starter.finish("GreeterPassedThrough", some_run())
    assert output == HELLO


# ---------------------------------------------------------------- the limits
@EITHER_WORKFLOW
async def test_limits_declared_on_the_action_are_the_ones_temporal_ran_it_under(serving, name):
    run = some_run()
    async with serving() as starter:
        await starter.finish(name, run)
        history = await starter.history(name, run)
    assert scheduled_with(history, vocabulary.RUN_ACTION) == ScheduledWith(
        start_to_close=timedelta(seconds=17),
        heartbeat=timedelta(seconds=9),
        max_attempts=4,
        first_wait=timedelta(seconds=0.5),
    )


@pytest.mark.parametrize(
    ("name", "limits"),
    [
        (
            "Greeter",
            ScheduledWith(timedelta(seconds=31), timedelta(seconds=13), 2, timedelta(seconds=0.2)),
        ),
        (
            "GreeterPassedThrough",
            ScheduledWith(timedelta(seconds=32), timedelta(seconds=14), 3, timedelta(seconds=0.3)),
        ),
    ],
)
async def test_limits_given_to_the_decorator_are_the_ones_temporal_ran_the_steps_under(
    serving, name, limits
):
    run = some_run()
    async with serving() as starter:
        await starter.finish(name, run)
        history = await starter.history(name, run)
    assert scheduled_with(history, vocabulary.RUN_GRAPH_STEP) == limits


async def test_action_that_fails_once_is_tried_again_with_the_same_action_id(serving):
    handlers.service.failures.append(RuntimeError("the greeting service is down"))
    async with serving() as starter:
        output = await starter.finish("Greeter", some_run())
    first, second = (key for _, key in handlers.service.calls)
    assert first == second
    assert output == HELLO


# ------------------------------------------------------------------ the hooks
async def test_hooks_run_around_the_graph_and_the_run_ends_by_forgetting_its_checkpoints(serving):
    run = some_run()
    async with serving() as starter:
        await starter.finish("Greeter", run)
        history = await starter.history("Greeter", run)
    assert [exchange.activity for exchange in exchanges(history)] == [
        "note_greeting",
        "run_graph_step",
        "run_action",
        "run_graph_step",
        "note_greeting",
        "forget_run",
    ]


async def test_hooks_are_given_the_run_and_what_the_graph_finished_with(serving):
    async with serving() as starter:
        await starter.finish("Greeter", some_run())
    assert handlers.notes == [Note("before", KIND), Note("after", "hello, Ada!")]


async def test_failed_hook_is_told_why_an_action_the_agent_did_not_declare_ended_the_run(serving):
    run = some_run()
    graph.subjects[run.run_id] = graph.ASKS_FOR_ANOTHER_AGENTS_ACTION
    async with serving() as starter:
        failure = await starter.fail("Greeter", run)
    assert isinstance(failure, ApplicationError)
    assert (failure.type, failure.non_retryable) == (vocabulary.UNKNOWN_ACTION, True)
    assert handlers.notes == [Note("before", KIND), Note("failed", NOT_DECLARED)]
    assert handlers.service.calls == []


async def test_failed_hook_is_told_why_a_payload_the_model_refuses_ended_the_run(serving):
    run = some_run()
    graph.subjects[run.run_id] = graph.ASKS_WITH_A_PAYLOAD_THAT_DOES_NOT_FIT
    async with serving() as starter:
        failure = await starter.fail("Greeter", run)
    assert isinstance(failure, ActivityError)
    assert isinstance(failure.cause, ApplicationError)
    assert failure.cause.type == vocabulary.INVALID_ACTION_PAYLOAD
    assert [note.step for note in handlers.notes] == ["before", "failed"]
    assert handlers.notes[1].detail.startswith("payload of action 'greet' does not fit")
    assert handlers.service.calls == []


async def test_failed_hook_is_told_that_a_cancelled_run_was_cancelled(serving):
    async with serving() as starter:
        await a_cancelled_run(starter, some_run())
    assert handlers.notes == [Note("before", KIND), Note("failed", vocabulary.RUN_TEXTS.cancelled)]


# ------------------------------------------------------------- the checkpoints
async def test_checkpoints_of_a_run_that_finished_are_forgotten(serving, forgetting, database):
    run = some_run()
    async with serving() as starter:
        await starter.finish("Greeter", run)
    assert forgetting.asked == [(thread_of(run), True)]
    assert not database.thread_has_checkpoints(thread_of(run))


async def test_checkpoints_of_a_run_that_failed_are_forgotten(serving, forgetting, database):
    run = some_run()
    graph.subjects[run.run_id] = graph.ASKS_FOR_ANOTHER_AGENTS_ACTION
    async with serving() as starter:
        await starter.fail("Greeter", run)
    assert forgetting.asked == [(thread_of(run), True)]
    assert not database.thread_has_checkpoints(thread_of(run))


async def test_checkpoints_of_a_run_that_was_cancelled_are_forgotten(serving, forgetting, database):
    run = some_run()
    async with serving() as starter:
        await a_cancelled_run(starter, run)
    assert forgetting.asked == [(thread_of(run), True)]
    assert not database.thread_has_checkpoints(thread_of(run))


async def test_run_that_finished_keeps_its_result_when_forgetting_its_checkpoints_fails(
    serving, forgetting
):
    forgetting.fails_with = ApplicationError("not forgotten", type="Refused", non_retryable=True)
    async with serving() as starter:
        output = await starter.finish("Greeter", some_run())
    assert output == HELLO


async def test_run_that_failed_ends_with_its_own_failure_when_forgetting_its_checkpoints_fails(
    serving, forgetting
):
    forgetting.fails_with = ApplicationError("not forgotten", type="Refused", non_retryable=True)
    run = some_run()
    graph.subjects[run.run_id] = graph.ASKS_FOR_ANOTHER_AGENTS_ACTION
    async with serving() as starter:
        failure = await starter.fail("Greeter", run)
    assert isinstance(failure, ApplicationError)
    assert failure.type == vocabulary.UNKNOWN_ACTION


# ---------------------------------------------------------------------- replay
@EITHER_WORKFLOW
async def test_history_of_a_run_that_finished_replays(serving, name):
    run = some_run()
    async with serving() as starter:
        await starter.finish(name, run)
        history = await starter.history(name, run)
    await Replayer(workflows=[WORKFLOWS[name]]).replay_workflow(history)


async def test_history_of_a_run_that_failed_replays(serving):
    run = some_run()
    graph.subjects[run.run_id] = graph.ASKS_FOR_ANOTHER_AGENTS_ACTION
    async with serving() as starter:
        await starter.fail("Greeter", run)
        history = await starter.history("Greeter", run)
    await Replayer(workflows=[Greeter]).replay_workflow(history)


async def test_history_of_a_run_that_was_cancelled_replays(serving):
    run = some_run()
    async with serving() as starter:
        await a_cancelled_run(starter, run)
        history = await starter.history("Greeter", run)
    await Replayer(workflows=[Greeter]).replay_workflow(history)
