"""The greeter's run actions on a real Temporal server: a run started, and a run waited for.

The greeter asks for ``GreetingChild`` -- a plain workflow on a task queue of its own
(``child.py``, served by the fixture in ``conftest.py``) -- through the two actions it declares:
one that starts the child and goes on (``start_a_greeting``), and one that starts it and waits
(``wait_for_a_greeting``). A test reads what a run made from the ``world`` it was wired with
(``world.prepared``, keyed by the action's id) and from the parent's history, which holds the
child's start: its name, its id and its input, identifiers and small values only.

Every wait for a child's result happens inside the ``serving`` block: the child is served by
the worker the block holds, and a child that is still running when the block ends is not
finished by anyone.
"""

import asyncio

import pytest
from scripted_history import ChildStarted, children_started, exchanges, readable
from scripted_rig import happened
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError
from temporalio.exceptions import CancelledError
from temporalio.service import RPCError
from temporalio.worker import Replayer

from dsl import graph
from dsl.child import CHILD_REFUSED
from dsl.definitions import (
    CHILD_TASK_QUEUE,
    CHILD_WORKFLOW,
    GREETERS_PROJECT,
    WaitForAGreeting,
)
from dsl.rig import cancel_and_let_it_be_seen, some_run
from dsl.wiring import GreeterWorld, Prepared
from dsl.workflow import Greeter
from openmarketer_worker.agent_runtime import driver, vocabulary


def the_child_of(world: GreeterWorld, history, run) -> tuple[ChildStarted, Prepared]:
    """The one child the parent started, and the run the prepare callable made for it."""
    ((action_id, prepared),) = world.prepared.items()
    (child,) = children_started(history)
    assert child.workflow_id == driver.child_workflow_id(run, action_id)
    return child, prepared


async def the_running_child(client: Client, world: GreeterWorld, run, *, within: float = 20):
    """The child's handle, once its run was prepared and the child is running.

    A test that has just started the parent waits for the prepare to have happened before it
    derives the child's id: the parent's first step runs on the worker.
    """

    async def running() -> str:
        while not world.prepared:
            await asyncio.sleep(0.05)
        (action_id,) = world.prepared
        child_id = driver.child_workflow_id(run, action_id)
        while True:
            try:
                description = await client.get_workflow_handle(child_id).describe()
            except RPCError:
                # The run is prepared, but the parent may not have issued the start yet.
                await asyncio.sleep(0.1)
                continue
            if description.status is WorkflowExecutionStatus.RUNNING:
                return child_id
            await asyncio.sleep(0.1)

    child_id = await asyncio.wait_for(running(), timeout=within)
    return client.get_workflow_handle(child_id)


# ------------------------------------------------------------------- a run waited for
async def test_a_run_a_parent_waits_for_gives_it_its_small_result(serving, world, temporal):
    run = some_run()
    world.subjects[run.run_id] = graph.WAITS_FOR_A_GREETING
    async with serving() as starter:
        output = await starter.finish("Greeter", run)
        history = await starter.history("Greeter", run)
        child, prepared = the_child_of(world, history, run)
        child_result = await asyncio.wait_for(
            temporal.get_workflow_handle(child.workflow_id).result(), timeout=20
        )
    # The child's result is the outcome, and the greeter's graph read it as the reply.
    assert output == {"summary": "hello, Ada!"}
    assert prepared.payload == WaitForAGreeting(name="Ada", outcome="greets")
    assert prepared.scope == GREETERS_PROJECT
    assert (child.workflow, child.task_queue) == (CHILD_WORKFLOW, CHILD_TASK_QUEUE)
    assert child.given == {
        "run_id": str(prepared.run_id),
        "name": "Ada",
        "outcome": "greets",
    }
    # And the child itself finished with its own small result, under the run record's id.
    assert child_result == {"greeting": "hello, Ada", "child_run_id": str(prepared.run_id)}


async def test_a_run_that_does_not_finish_is_told_to_the_graph_as_a_failed_outcome(serving, world):
    run = some_run()
    world.subjects[run.run_id] = graph.WAITS_FOR_A_FAILING_GREETING
    async with serving() as starter:
        output = await starter.finish("Greeter", run)
        history = await starter.history("Greeter", run)
    # The graph decided to give up: the action's outcome failed, in the runtime's fixed words.
    assert output == {"summary": "nobody was greeted"}
    steps = [exchange for exchange in exchanges(history) if exchange.activity == "run_graph_step"]
    resume = steps[-1].given["resume"]
    assert resume["succeeded"] is False
    assert resume["error"] == vocabulary.CHILD_RUN_DID_NOT_FINISH
    # The child's own failure, with its type and message, stays in the child's own record.
    child, _ = the_child_of(world, history, run)
    assert child.workflow == CHILD_WORKFLOW


async def test_the_childs_own_failure_stays_in_the_childs_record(serving, world, temporal):
    run = some_run()
    world.subjects[run.run_id] = graph.WAITS_FOR_A_FAILING_GREETING
    async with serving() as starter:
        await starter.finish("Greeter", run)
        history = await starter.history("Greeter", run)
        child, _ = the_child_of(world, history, run)
        with pytest.raises(WorkflowFailureError) as ended:
            await asyncio.wait_for(
                temporal.get_workflow_handle(child.workflow_id).result(), timeout=20
            )
    assert getattr(ended.value.__cause__, "type", None) == CHILD_REFUSED
    # The child's failure has its own words in its own record; the parent's graph was told
    # the runtime's fixed sentence instead.
    errors = [
        (exchange.given.get("resume") or {}).get("error", "")
        for exchange in exchanges(history)
        if exchange.activity == "run_graph_step" and exchange.given
    ]
    assert "the child could not greet" not in errors
    assert vocabulary.CHILD_RUN_DID_NOT_FINISH in errors


# ----------------------------------------------------------------- a run that is started
async def test_a_run_a_parent_starts_is_told_to_the_graph_with_its_id(serving, world, temporal):
    run = some_run()
    world.subjects[run.run_id] = graph.STARTS_A_GREETING
    async with serving() as starter:
        output = await starter.finish("Greeter", run)
        history = await starter.history("Greeter", run)
        child, prepared = the_child_of(world, history, run)
        child_result = await asyncio.wait_for(
            temporal.get_workflow_handle(child.workflow_id).result(), timeout=20
        )
    # The graph goes on with the run's id, without waiting for the child.
    assert output == {"summary": f"{prepared.run_id}!"}
    assert (child.workflow, child.task_queue) == (CHILD_WORKFLOW, CHILD_TASK_QUEUE)
    assert child.given == {
        "run_id": str(prepared.run_id),
        "name": "Ada",
        "outcome": "greets",
    }
    assert child_result == {"greeting": "hello, Ada", "child_run_id": str(prepared.run_id)}


async def test_a_run_a_parent_starts_goes_on_when_its_parent_is_cancelled(serving, world, temporal):
    run = some_run()
    world.subjects[run.run_id] = graph.STARTS_A_SLOW_GREETING_AND_GREETS
    world.service.holds = True  # the parent waits in the greeting after the run it started
    async with serving() as starter:
        handle = await starter.start("Greeter", run)
        await happened(world.service.started)
        try:
            await handle.cancel()
            with pytest.raises(WorkflowFailureError) as stopped:
                await asyncio.wait_for(handle.result(), timeout=20)
        finally:
            world.service.release.set()
        # The run was only started by this one: nobody waits for it, so it finishes on its own
        # even though the parent's cancellation was the last thing that happened to the parent.
        ((_, prepared),) = world.prepared.items()
        (action_id,) = world.prepared
        child_result = await asyncio.wait_for(
            temporal.get_workflow_handle(driver.child_workflow_id(run, action_id)).result(),
            timeout=20,
        )
    assert isinstance(stopped.value.__cause__, CancelledError)
    assert child_result == {"greeting": "hello, Ada", "child_run_id": str(prepared.run_id)}


async def test_a_run_a_parent_waits_for_is_cancelled_with_it(serving, world, temporal):
    run = some_run()
    world.subjects[run.run_id] = graph.WAITS_FOR_A_GREETING_THAT_NEVER_COMES
    async with serving() as starter:
        handle = await starter.start("Greeter", run)
        child = await the_running_child(temporal, world, run)
        await cancel_and_let_it_be_seen(handle)
        with pytest.raises(WorkflowFailureError) as stopped:
            await asyncio.wait_for(handle.result(), timeout=30)
        with pytest.raises(WorkflowFailureError) as stopped_child:
            await asyncio.wait_for(child.result(), timeout=30)
    # The run the graph waited for was cancelled with its parent: the parent's own cancellation
    # is what said nobody needs its result any more.
    assert isinstance(stopped.value.__cause__, CancelledError)
    assert isinstance(stopped_child.value.__cause__, CancelledError)


# ------------------------------------------------------- a prepare that is tried again
async def test_a_prepare_that_fails_once_makes_one_run_and_starts_it_once(serving, world, temporal):
    run = some_run()
    world.subjects[run.run_id] = graph.WAITS_FOR_A_GREETING
    # The callable makes the run's record and then fails: the attempt is tried again, with
    # the same action id, and must find the run it made instead of making a second.
    world.prepare_failures.append(RuntimeError("the connection dropped mid-write"))
    async with serving() as starter:
        output = await starter.finish("Greeter", run)
        history = await starter.history("Greeter", run)
        child, prepared = the_child_of(world, history, run)
        child_result = await asyncio.wait_for(
            temporal.get_workflow_handle(child.workflow_id).result(), timeout=20
        )
    assert output == {"summary": "hello, Ada!"}
    assert len(world.prepared) == 1
    assert len(children_started(history)) == 1
    assert child_result == {"greeting": "hello, Ada", "child_run_id": str(prepared.run_id)}


async def test_a_run_that_is_only_started_is_made_once_when_the_prepare_is_tried_again(
    serving, world, temporal
):
    run = some_run()
    world.subjects[run.run_id] = graph.STARTS_A_GREETING
    world.prepare_failures.append(RuntimeError("the connection dropped mid-write"))
    async with serving() as starter:
        await starter.finish("Greeter", run)
        history = await starter.history("Greeter", run)
        child, prepared = the_child_of(world, history, run)
        child_result = await asyncio.wait_for(
            temporal.get_workflow_handle(child.workflow_id).result(), timeout=20
        )
    assert len(world.prepared) == 1
    assert len(children_started(history)) == 1
    assert child_result == {"greeting": "hello, Ada", "child_run_id": str(prepared.run_id)}


# ----------------------------------------------------------------- what the history holds
async def test_the_history_holds_the_action_ids_and_the_run_ids_and_no_child_decision(
    serving, world
):
    run = some_run()
    world.subjects[run.run_id] = graph.WAITS_FOR_A_FAILING_GREETING
    async with serving() as starter:
        await starter.finish("Greeter", run)
        history = await starter.history("Greeter", run)
    ((action_id, prepared),) = world.prepared.items()
    child, _ = the_child_of(world, history, run)
    text = readable(history)
    assert str(prepared.run_id) in text
    assert action_id in text
    assert child.workflow_id in text
    # What the child failed with is its own record's: the parent's history holds the fact of
    # the child's failure, as the engine records every child's end, and no second copy of the
    # child's message beyond it.
    assert text.count("the child could not greet") <= 1


async def test_the_history_of_a_run_with_run_actions_replays(serving, world):
    run = some_run()
    world.subjects[run.run_id] = graph.WAITS_FOR_A_GREETING
    async with serving() as starter:
        await starter.finish("Greeter", run)
        history = await starter.history("Greeter", run)
    await Replayer(workflows=[Greeter]).replay_workflow(history)
