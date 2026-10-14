"""Tests for what ``AgentWorker`` collects, and what it refuses when the worker starts.

A fresh agent is declared in each test that needs one: an agent is plain data, and the check
under test is what its worker makes of the declarations.
"""

import uuid
from datetime import timedelta

import pytest
from pydantic import BaseModel, ConfigDict
from scripted_graphs import RaisingGraph, ScriptedGraph
from temporalio import workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from dsl.definitions import GREETER_TEXTS, Unlisted, elsewhere
from dsl.wiring import LONGEST_RUN, TASK_QUEUE, GreeterWorld, wire
from dsl.workflow import GRAPH, Greeter
from openmarketer_core.agent_runtime.actions import ActionEffect
from openmarketer_core.agent_runtime.agent import Agent
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_worker.agent_runtime.agent_worker import AgentWorker, InvalidAgentWorker
from openmarketer_worker.agent_runtime.vocabulary import STEP_REFUSED, StepInput
from openmarketer_worker.agent_runtime.workflow_dsl import agent_workflow
from openmarketer_worker.checkpoint_cleanup import CheckpointCleanup


class Plain(BaseModel):
    """A model that no agent declared as an action."""

    model_config = ConfigDict(extra="forbid")


@agent_workflow(elsewhere, name="Elsewhere", graph=GRAPH)
class Elsewhere:
    """The workflow of an agent that is not the one under test."""


@workflow.defn(name="ByHand")
class ByHand:
    """A Temporal workflow that no ``agent_workflow`` made."""

    @workflow.run
    async def run(self) -> None: ...


def an_agent_with_the_action_send() -> tuple[Agent, type[BaseModel]]:
    agent = Agent("dsl-fresh", state=dict)

    @agent.action("send")
    class Send(BaseModel):
        model_config = ConfigDict(extra="forbid")

    return agent, Send


def a_worker_with_a_handler(*, run_ended=None) -> AgentWorker:
    agent, send = an_agent_with_the_action_send()
    worker = AgentWorker(agent, task_queue="q")
    worker.handler(send)(lambda payload, context: {})
    if run_ended is not None:
        worker.run_ended(run_ended)
    return worker


# --------------------------------------------------------------- registering handlers
def test_handler_is_returned_as_it_is_so_that_it_is_tested_as_a_plain_function():
    agent, send = an_agent_with_the_action_send()

    def handler(payload, context):
        return {}

    assert AgentWorker(agent, task_queue="q").handler(send)(handler) is handler


def test_handler_for_a_model_the_agent_did_not_declare_is_refused_naming_the_model():
    agent, _ = an_agent_with_the_action_send()
    with pytest.raises(InvalidAgentWorker, match="model Plain is not the payload of an action"):
        AgentWorker(agent, task_queue="q").handler(Plain)


def test_handler_for_a_model_another_agent_declared_is_refused_naming_the_model():
    agent, _ = an_agent_with_the_action_send()
    with pytest.raises(InvalidAgentWorker, match="model Unlisted is not the payload"):
        AgentWorker(agent, task_queue="q").handler(Unlisted)


def test_second_handler_for_one_action_is_refused_naming_the_action_and_both_handlers():
    agent, send = an_agent_with_the_action_send()
    worker = AgentWorker(agent, task_queue="q")

    @worker.handler(send)
    def first(payload, context):
        return {}

    with pytest.raises(InvalidAgentWorker, match="'send'.*two handlers.*first.*second"):

        @worker.handler(send)
        def second(payload, context):
            return {}


def test_handler_that_is_async_is_refused_because_a_handler_blocks_in_a_thread():
    agent, send = an_agent_with_the_action_send()
    with pytest.raises(InvalidAgentWorker, match="'send' is an async def"):

        @AgentWorker(agent, task_queue="q").handler(send)  # pyright: ignore[reportArgumentType]
        async def handler(payload, context):
            return {}


# --------------------------------------------------------------- the end of a run
def test_function_that_releases_what_a_run_left_is_returned_as_it_is():
    agent, _ = an_agent_with_the_action_send()

    def release(run):
        return None

    assert AgentWorker(agent, task_queue="q").run_ended(release) is release


def test_second_function_for_the_end_of_a_run_is_refused_naming_both():
    agent, _ = an_agent_with_the_action_send()
    worker = AgentWorker(agent, task_queue="q")

    @worker.run_ended
    def first(run):
        return None

    with pytest.raises(InvalidAgentWorker, match="'dsl-fresh'.*two run-ended.*first.*second"):

        @worker.run_ended
        def second(run):
            return None


def test_function_for_the_end_of_a_run_that_is_async_is_refused_because_it_blocks_in_a_thread():
    agent, _ = an_agent_with_the_action_send()
    with pytest.raises(InvalidAgentWorker, match="'dsl-fresh' is an async def"):

        @AgentWorker(agent, task_queue="q").run_ended  # pyright: ignore[reportArgumentType]
        async def release(run):
            return None


# --------------------------------------------------------------------- registration
def test_action_with_no_handler_is_refused_when_the_worker_starts_naming_the_action(checkpoints):
    agent, _ = an_agent_with_the_action_send()
    worker = AgentWorker(agent, task_queue="q")
    with pytest.raises(InvalidAgentWorker, match="'send' with no handler"):
        worker.registration(
            checkpoints=checkpoints,
            longest_run=LONGEST_RUN,
            graph=ScriptedGraph([]),
            workflows=[Greeter],
        )


@pytest.mark.parametrize("candidate", [Plain, ByHand], ids=lambda candidate: candidate.__name__)
def test_class_that_agent_workflow_did_not_make_is_refused_naming_the_class(checkpoints, candidate):
    with pytest.raises(InvalidAgentWorker, match=f"{candidate.__name__} is not the workflow of"):
        a_worker_with_a_handler().registration(
            checkpoints=checkpoints,
            longest_run=LONGEST_RUN,
            graph=ScriptedGraph([]),
            workflows=[candidate],
        )


def test_workflow_of_another_agent_is_refused_naming_the_class_and_both_agents(checkpoints):
    with pytest.raises(InvalidAgentWorker, match="Elsewhere.*'dsl-elsewhere'.*'dsl-fresh'"):
        a_worker_with_a_handler().registration(
            checkpoints=checkpoints,
            longest_run=LONGEST_RUN,
            graph=ScriptedGraph([]),
            workflows=[Elsewhere],
        )


def test_registration_holds_the_queue_the_workflows_and_every_activity_the_run_needs(checkpoints):
    registration = wire(GreeterWorld(), checkpoints)
    assert registration.task_queue == TASK_QUEUE
    assert [workflow.__name__ for workflow in registration.workflows] == [
        "Greeter",
        "GreeterPassedThrough",
        "GreeterWithLimitsOfItsOwn",
        "GreeterOfARequest",
        "GreeterInItsOwnWords",
    ]
    assert [activity.__name__ for activity in registration.activities] == [
        "run_graph_step",
        "run_action",
        "forget_run",
        "note",
        "limits_of_the_run",
    ]


def test_registration_holds_the_cleanups_it_was_given_and_the_one_of_its_kind(checkpoints):
    cleanup = CheckpointCleanup(runs="greetings", remove=lambda session: 0)
    registration = a_worker_with_a_handler().registration(
        checkpoints=checkpoints,
        longest_run=LONGEST_RUN,
        graph=ScriptedGraph([]),
        workflows=[],
        checkpoint_cleanups=[cleanup],
    )
    given, of_its_kind = registration.checkpoint_cleanups
    assert given == cleanup
    assert of_its_kind.runs.startswith("dsl-fresh runs untouched")


def test_agent_with_no_cleanup_of_its_own_still_has_its_left_over_threads_removed(checkpoints):
    registration = a_worker_with_a_handler().registration(
        checkpoints=checkpoints, longest_run=LONGEST_RUN, graph=ScriptedGraph([]), workflows=[]
    )
    [of_its_kind] = registration.checkpoint_cleanups
    assert of_its_kind.runs.startswith("dsl-fresh runs untouched")


@pytest.mark.parametrize("longest_run", [timedelta(0), timedelta(seconds=-1)])
def test_longest_run_of_no_time_is_refused(checkpoints, longest_run):
    with pytest.raises(InvalidAgentWorker, match="longer than nothing"):
        a_worker_with_a_handler().registration(
            checkpoints=checkpoints, longest_run=longest_run, graph=ScriptedGraph([]), workflows=[]
        )


def test_longest_run_shorter_than_a_wait_for_a_person_is_refused(checkpoints, sessions):
    agent = Agent("dsl-held", state=dict)

    @agent.action("publish", effect=ActionEffect.PUBLISHES, approval_wait=2 * 60 * 60)
    class Publish(BaseModel):
        model_config = ConfigDict(extra="forbid")

    worker = AgentWorker(agent, task_queue="q")
    worker.handler(Publish)(lambda payload, context: {})
    with pytest.raises(InvalidAgentWorker, match="'publish'.*checkpoints would be removed"):
        worker.registration(
            checkpoints=checkpoints,
            longest_run=timedelta(hours=1),
            graph=ScriptedGraph([]),
            workflows=[],
            sessions=sessions,
        )


async def test_activities_registered_serve_the_runs_of_the_agent_and_no_other_kind(checkpoints):
    registration = a_worker_with_a_handler().registration(
        checkpoints=checkpoints, longest_run=LONGEST_RUN, graph=ScriptedGraph([]), workflows=[]
    )
    run_graph_step, _, forget_run = registration.activities[:3]
    stranger = GraphRunRef(kind="dsl-elsewhere", run_id=uuid.uuid4())
    for refused in (
        ActivityEnvironment().run(run_graph_step, StepInput(stranger, None)),
        ActivityEnvironment().run(forget_run, stranger),
    ):
        with pytest.raises(ApplicationError) as failure:
            await refused
        assert (failure.value.type, failure.value.non_retryable) == (STEP_REFUSED, True)


async def test_activities_registered_word_a_failure_nobody_worded_in_the_texts_given(checkpoints):
    registration = a_worker_with_a_handler().registration(
        checkpoints=checkpoints,
        longest_run=LONGEST_RUN,
        graph=RaisingGraph(RuntimeError("a message written for a log")),
        workflows=[],
        texts=GREETER_TEXTS,
    )
    run = GraphRunRef(kind="dsl-fresh", run_id=uuid.uuid4())
    with pytest.raises(ApplicationError) as failure:
        await ActivityEnvironment().run(registration.activities[0], StepInput(run, None))
    assert failure.value.message == GREETER_TEXTS.stopped_unexpectedly


async def test_activities_registered_tell_the_function_for_the_end_of_a_run_of_each_forgotten_run(
    checkpoints,
):
    released: list[GraphRunRef] = []
    worker = a_worker_with_a_handler(run_ended=released.append)
    registration = worker.registration(
        checkpoints=checkpoints, longest_run=LONGEST_RUN, graph=ScriptedGraph([]), workflows=[]
    )
    run = GraphRunRef(kind="dsl-fresh", run_id=uuid.uuid4())
    await ActivityEnvironment().run(registration.activities[2], run)
    assert released == [run]
