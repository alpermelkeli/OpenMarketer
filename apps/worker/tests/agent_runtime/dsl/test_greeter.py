"""An agent written only with the decorators, on a real Temporal server and real checkpoints.

The greeter (``definitions.py``, ``graph.py``, ``wiring.py``, ``workflow.py``) asks for one
action and reports what it was told. Each test starts its workflow by name, the way the API
does, on a worker serving the agent's registration (``conftest.py``), and reaches the outside
world of the run through the ``world`` it was wired with.
"""

import asyncio
import uuid
from datetime import timedelta

import pytest
from scripted_history import ScheduledWith, completed, exchanges, scheduled_with
from scripted_rig import a_thread_with_a_checkpoint, cause_of, happened
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError
from temporalio.worker import Replayer

from dsl import graph
from dsl.definitions import (
    GREETER_TEXTS,
    KIND,
    NOTE_REFUSED,
    Greet,
    GreetRequest,
    Limits,
    Note,
)
from dsl.rig import Starter, cancel_and_let_it_be_seen, some_run, thread_of
from dsl.wiring import GreeterWorld
from dsl.workflow import Greeter, GreeterInItsOwnWords, GreeterOfARequest
from dsl.workflow_passthrough import GreeterPassedThrough
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_worker.agent_runtime import vocabulary

WORKFLOWS = {"Greeter": Greeter, "GreeterPassedThrough": GreeterPassedThrough}
EITHER_WORKFLOW = pytest.mark.parametrize("name", list(WORKFLOWS))

HELLO = {"summary": "hello, Ada!"}
NOT_DECLARED = "action 'unlisted' is not one of the allowed actions"


def a_request(run: GraphRunRef, *, patience: float = 45) -> GreetRequest:
    return GreetRequest(run_id=run.run_id, requested_by="grace", patience_seconds=patience)


async def a_cancelled_run(
    starter: Starter, world: GreeterWorld, run: GraphRunRef, workflow_name: str = "Greeter"
) -> None:
    """Cancels a run of the greeter while its action works, and returns once it has ended."""
    world.service.holds = True
    handle = await starter.start(workflow_name, run)
    try:
        await happened(world.service.started)
        await handle.cancel()
        with pytest.raises(WorkflowFailureError) as stopped:
            await asyncio.wait_for(handle.result(), timeout=20)
    finally:
        world.service.release.set()
    assert isinstance(stopped.value.__cause__, CancelledError)


# ------------------------------------------------------------------- the run
async def test_run_pauses_at_the_action_and_finishes_with_what_the_agent_reports(serving):
    async with serving() as starter:
        output = await starter.finish("Greeter", some_run())
    # The result projection, and not the graph's state, which held the reply as well.
    assert output == HELLO


async def test_handler_gets_the_validated_payload_and_the_id_of_its_action(serving, world):
    run = some_run()
    world.subjects[run.run_id] = "Grace"
    async with serving() as starter:
        await starter.finish("Greeter", run)
    ((payload, key),) = world.service.calls
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


async def test_limits_a_hook_returns_are_the_ones_temporal_ran_the_steps_under(serving, world):
    world.limits = Limits(attempt_timeout_seconds=44)
    run = some_run()
    async with serving() as starter:
        output = await starter.finish("GreeterWithLimitsOfItsOwn", run)
        history = await starter.history("GreeterWithLimitsOfItsOwn", run)
    assert output == HELLO
    assert scheduled_with(history, vocabulary.RUN_GRAPH_STEP) == ScheduledWith(
        timedelta(seconds=44), timedelta(seconds=13), 2, timedelta(seconds=0.2)
    )


async def test_action_that_fails_once_is_tried_again_with_the_same_action_id(serving, world):
    world.service.failures.append(RuntimeError("the greeting service is down"))
    async with serving() as starter:
        output = await starter.finish("Greeter", some_run())
    first, second = (key for _, key in world.service.calls)
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


async def test_hooks_are_given_the_run_and_what_the_graph_finished_with(serving, world):
    async with serving() as starter:
        await starter.finish("Greeter", some_run())
    assert world.notes == [Note("before", KIND), Note("after", "hello, Ada!")]


async def test_failed_hook_is_told_why_an_action_the_agent_did_not_declare_ended_the_run(
    serving, world
):
    run = some_run()
    world.subjects[run.run_id] = graph.ASKS_FOR_ANOTHER_AGENTS_ACTION
    async with serving() as starter:
        failure = await starter.fail("Greeter", run)
    assert isinstance(failure, ApplicationError)
    assert (failure.type, failure.non_retryable) == (vocabulary.UNKNOWN_ACTION, True)
    assert world.notes == [Note("before", KIND), Note("failed", NOT_DECLARED)]
    assert world.service.calls == []


async def test_failed_hook_is_told_why_a_payload_the_model_refuses_ended_the_run(serving, world):
    run = some_run()
    world.subjects[run.run_id] = graph.ASKS_WITH_A_PAYLOAD_THAT_DOES_NOT_FIT
    async with serving() as starter:
        failure = await starter.fail("Greeter", run)
    assert isinstance(failure, ActivityError)
    assert isinstance(failure.cause, ApplicationError)
    assert failure.cause.type == vocabulary.INVALID_ACTION_PAYLOAD
    assert [note.step for note in world.notes] == ["before", "failed"]
    assert world.notes[1].detail.startswith("payload of action 'greet' does not fit")
    assert world.service.calls == []


async def test_failed_hook_is_told_that_a_cancelled_run_was_cancelled(serving, world):
    async with serving() as starter:
        await a_cancelled_run(starter, world, some_run())
    assert world.notes == [Note("before", KIND), Note("failed", vocabulary.RUN_TEXTS.cancelled)]


async def test_failed_hook_is_called_when_before_fails(serving, world):
    world.refusals["before"] = ApplicationError("no note", type="NoteRefused", non_retryable=True)
    async with serving() as starter:
        failure = await starter.fail("Greeter", some_run())
    assert cause_of(failure).type == "NoteRefused"
    assert [note.step for note in world.notes] == ["failed"]


async def test_failed_hook_is_called_when_after_fails(serving, world):
    world.refusals["after"] = ApplicationError("no note", type="NoteRefused", non_retryable=True)
    async with serving() as starter:
        failure = await starter.fail("Greeter", some_run())
    assert cause_of(failure).type == "NoteRefused"
    assert [note.step for note in world.notes] == ["before", "failed"]


async def test_run_whose_failed_hook_fails_ends_with_that_failure_and_still_forgets_its_checkpoints(
    serving, world, forgetting, database
):
    run = some_run()
    world.subjects[run.run_id] = graph.ASKS_FOR_ANOTHER_AGENTS_ACTION
    world.refusals["failed"] = ApplicationError("no note", type="NoteRefused", non_retryable=True)
    async with serving() as starter:
        failure = await starter.fail("Greeter", run)
    assert cause_of(failure).type == "NoteRefused"
    assert forgetting.asked == [(thread_of(run), True)]
    assert not database.thread_has_checkpoints(thread_of(run))


async def test_failed_hook_is_not_told_the_message_of_an_error_nobody_worded(serving, world):
    leaking = "could not connect to the database at db-1.internal:5432"
    world.refusals["before"] = ApplicationError(
        leaking, type="OperationalError", non_retryable=True
    )
    async with serving() as starter:
        await starter.fail("Greeter", some_run())
    assert world.notes == [Note("failed", vocabulary.RUN_TEXTS.stopped_unexpectedly)]


# ----------------------------------------------------------------- another agent's run
async def test_workflow_started_with_a_run_of_another_agent_refuses_it_and_touches_nothing(
    serving, world, forgetting, database, checkpoints
):
    other = GraphRunRef(kind="dsl-elsewhere", run_id=uuid.uuid4())
    a_thread_with_a_checkpoint(checkpoints, thread_of(other))
    async with serving() as starter:
        failure = await starter.fail("Greeter", other)
    # Nothing ran: no hook, no step, and the other agent's run kept its checkpoints.
    assert world.notes == []
    assert forgetting.asked == []
    assert database.thread_has_checkpoints(thread_of(other))
    assert isinstance(failure, ApplicationError)
    assert (failure.type, failure.non_retryable) == (vocabulary.RUN_OF_ANOTHER_KIND, True)


# ------------------------------------------------------------- the checkpoints
async def test_checkpoints_of_a_run_that_finished_are_forgotten(serving, forgetting, database):
    run = some_run()
    async with serving() as starter:
        await starter.finish("Greeter", run)
    assert forgetting.asked == [(thread_of(run), True)]
    assert not database.thread_has_checkpoints(thread_of(run))


async def test_checkpoints_of_a_run_that_failed_are_forgotten(serving, forgetting, database, world):
    run = some_run()
    world.subjects[run.run_id] = graph.ASKS_FOR_ANOTHER_AGENTS_ACTION
    async with serving() as starter:
        await starter.fail("Greeter", run)
    assert forgetting.asked == [(thread_of(run), True)]
    assert not database.thread_has_checkpoints(thread_of(run))


async def test_checkpoints_of_a_run_that_was_cancelled_are_forgotten(
    serving, forgetting, database, world
):
    run = some_run()
    async with serving() as starter:
        await a_cancelled_run(starter, world, run)
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
    serving, forgetting, world
):
    forgetting.fails_with = ApplicationError("not forgotten", type="Refused", non_retryable=True)
    run = some_run()
    world.subjects[run.run_id] = graph.ASKS_FOR_ANOTHER_AGENTS_ACTION
    async with serving() as starter:
        failure = await starter.fail("Greeter", run)
    assert isinstance(failure, ApplicationError)
    assert failure.type == vocabulary.UNKNOWN_ACTION


# --------------------------------------------------- a cancellation that comes late
async def cancelled_while_its_failure_is_recorded(
    starter: Starter, world: GreeterWorld, run: GraphRunRef
) -> BaseException | None:
    """Cancels a run that failed while its ``failed`` hook runs; what the workflow ended with."""
    world.subjects[run.run_id] = graph.ASKS_FOR_ANOTHER_AGENTS_ACTION
    recording = world.hold_the_note_of("failed")
    handle = await starter.start("Greeter", run)
    await asyncio.wait_for(recording.arrived.wait(), timeout=20)
    await cancel_and_let_it_be_seen(handle)
    recording.release.set()
    with pytest.raises(WorkflowFailureError) as ended:
        await asyncio.wait_for(handle.result(), timeout=20)
    return ended.value.__cause__


async def test_run_cancelled_while_its_failure_is_recorded_ends_with_its_failure_and_is_recorded(
    serving, world, database
):
    run = some_run()
    async with serving() as starter:
        failure = await cancelled_while_its_failure_is_recorded(starter, world, run)
        history = await starter.history("Greeter", run)
    # The workflow ended as the run did, and not as cancelled; the record was written before
    # the checkpoints were forgotten, and the workflow waited for both.
    assert isinstance(failure, ApplicationError)
    assert failure.type == vocabulary.UNKNOWN_ACTION
    assert world.notes == [Note("before", KIND), Note("failed", NOT_DECLARED)]
    assert completed(history) == ["note_greeting", "run_graph_step", "note_greeting", "forget_run"]
    assert not database.thread_has_checkpoints(thread_of(run))


async def test_run_cancelled_while_its_checkpoints_are_forgotten_keeps_its_result(
    serving, forgetting, database
):
    run = some_run()
    forgetting.holds = True
    async with serving() as starter:
        handle = await starter.start("Greeter", run)
        await happened(forgetting.arrived)
        try:
            await cancel_and_let_it_be_seen(handle)
        finally:
            forgetting.release.set()
        output = await asyncio.wait_for(handle.result(), timeout=20)
        history = await starter.history("Greeter", run)
    assert output == HELLO
    assert completed(history)[-1] == "forget_run"
    assert not database.thread_has_checkpoints(thread_of(run))


# -------------------------------------------------------- an input of the workflow's own
async def test_workflow_started_with_an_input_of_its_own_finishes_with_what_the_agent_reports(
    serving,
):
    run = some_run()
    async with serving() as starter:
        output = await starter.finish("GreeterOfARequest", run, given=a_request(run))
    assert output == HELLO


async def test_run_of_an_input_of_the_workflows_own_is_the_one_whose_thread_is_forgotten(
    serving, world, forgetting
):
    run = some_run()
    async with serving() as starter:
        await starter.finish("GreeterOfARequest", run, given=a_request(run))
    ((_, key),) = world.service.calls
    assert key.startswith(f"{thread_of(run)}:")
    assert forgetting.asked == [(thread_of(run), True)]


async def test_hooks_are_given_the_input_the_workflow_was_started_with(serving, world):
    run = some_run()
    async with serving() as starter:
        await starter.finish("GreeterOfARequest", run, given=a_request(run))
    assert world.notes == [Note("before", "grace"), Note("after", "grace: hello, Ada!")]


async def test_failed_hook_is_given_the_input_the_workflow_was_started_with(serving, world):
    run = some_run()
    world.subjects[run.run_id] = graph.ASKS_FOR_ANOTHER_AGENTS_ACTION
    async with serving() as starter:
        await starter.fail("GreeterOfARequest", run, given=a_request(run))
    assert world.notes == [Note("before", "grace"), Note("failed", f"grace: {NOT_DECLARED}")]


async def test_limits_a_hook_makes_from_its_input_are_the_ones_temporal_ran_the_steps_under(
    serving,
):
    run = some_run()
    async with serving() as starter:
        await starter.finish("GreeterOfARequest", run, given=a_request(run, patience=47))
        history = await starter.history("GreeterOfARequest", run)
    assert scheduled_with(history, vocabulary.RUN_GRAPH_STEP) == ScheduledWith(
        timedelta(seconds=47), timedelta(seconds=13), 2, timedelta(seconds=0.2)
    )


# ------------------------------------------------------------ a failure told in its words
async def test_failed_hook_is_told_the_texts_of_the_agent_for_an_error_nobody_worded(
    serving, world
):
    world.refusals["before"] = ApplicationError("db-1 refused", type="OperationalError")
    async with serving() as starter:
        await starter.fail("GreeterInItsOwnWords", some_run())
    assert world.notes == [Note("failed", GREETER_TEXTS.stopped_unexpectedly)]


async def test_failed_hook_is_told_the_message_of_a_type_the_agent_declared_worded(serving, world):
    refused = ApplicationError("the greeting was refused", type=NOTE_REFUSED, non_retryable=True)
    world.refusals["before"] = refused
    async with serving() as starter:
        await starter.fail("GreeterInItsOwnWords", some_run())
    assert world.notes == [Note("failed", "the greeting was refused")]


async def test_failed_hook_is_not_told_the_message_of_a_type_no_agent_declared_worded(
    serving, world
):
    refused = ApplicationError("the greeting was refused", type=NOTE_REFUSED, non_retryable=True)
    world.refusals["before"] = refused
    async with serving() as starter:
        await starter.fail("Greeter", some_run())
    assert world.notes == [Note("failed", vocabulary.RUN_TEXTS.stopped_unexpectedly)]


async def test_failed_hook_is_told_the_cancelled_text_of_the_agent_for_a_cancelled_run(
    serving, world
):
    async with serving() as starter:
        await a_cancelled_run(starter, world, some_run(), "GreeterInItsOwnWords")
    assert world.notes == [Note("before", KIND), Note("failed", GREETER_TEXTS.cancelled)]


# ------------------------------------------------ what the agent is told when a run ends
async def test_agent_is_told_once_of_a_run_that_finished(serving, world):
    run = some_run()
    async with serving() as starter:
        await starter.finish("Greeter", run)
    assert world.released == [run]


async def test_agent_is_told_once_of_a_run_that_failed(serving, world):
    run = some_run()
    world.subjects[run.run_id] = graph.ASKS_FOR_ANOTHER_AGENTS_ACTION
    async with serving() as starter:
        await starter.fail("Greeter", run)
    assert world.released == [run]


async def test_agent_is_told_once_of_a_run_that_was_cancelled(serving, world):
    run = some_run()
    async with serving() as starter:
        await a_cancelled_run(starter, world, run)
    assert world.released == [run]


async def test_agent_is_told_of_no_run_it_refused_as_another_agents(serving, world):
    async with serving() as starter:
        await starter.fail("Greeter", GraphRunRef(kind="dsl-elsewhere", run_id=uuid.uuid4()))
    assert world.released == []


async def test_run_keeps_its_result_when_what_it_left_on_disk_cannot_be_released(serving, world):
    world.release_fails_with = OSError("the disk is full")
    async with serving() as starter:
        output = await starter.finish("Greeter", some_run())
    assert output == HELLO


# ---------------------------------------------------------------------- replay
@EITHER_WORKFLOW
async def test_history_of_a_run_that_finished_replays(serving, name):
    run = some_run()
    async with serving() as starter:
        await starter.finish(name, run)
        history = await starter.history(name, run)
    await Replayer(workflows=[WORKFLOWS[name]]).replay_workflow(history)


async def test_history_of_a_run_that_failed_replays(serving, world):
    run = some_run()
    world.subjects[run.run_id] = graph.ASKS_FOR_ANOTHER_AGENTS_ACTION
    async with serving() as starter:
        await starter.fail("Greeter", run)
        history = await starter.history("Greeter", run)
    await Replayer(workflows=[Greeter]).replay_workflow(history)


async def test_history_of_a_run_that_was_cancelled_replays(serving, world):
    run = some_run()
    async with serving() as starter:
        await a_cancelled_run(starter, world, run)
        history = await starter.history("Greeter", run)
    await Replayer(workflows=[Greeter]).replay_workflow(history)


async def test_history_of_a_run_started_with_an_input_of_its_own_replays(serving):
    run = some_run()
    async with serving() as starter:
        await starter.finish("GreeterOfARequest", run, given=a_request(run))
        history = await starter.history("GreeterOfARequest", run)
    await Replayer(workflows=[GreeterOfARequest]).replay_workflow(history)


async def test_history_of_a_run_told_in_the_words_of_its_agent_replays(serving, world):
    world.refusals["before"] = ApplicationError("db-1 refused", type="OperationalError")
    run = some_run()
    async with serving() as starter:
        await starter.fail("GreeterInItsOwnWords", run)
        history = await starter.history("GreeterInItsOwnWords", run)
    await Replayer(workflows=[GreeterInItsOwnWords]).replay_workflow(history)


async def test_history_of_a_run_cancelled_while_its_failure_was_recorded_replays(serving, world):
    run = some_run()
    async with serving() as starter:
        await cancelled_while_its_failure_is_recorded(starter, world, run)
        history = await starter.history("Greeter", run)
    await Replayer(workflows=[Greeter]).replay_workflow(history)
