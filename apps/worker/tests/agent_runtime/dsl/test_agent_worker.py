"""Tests for what ``AgentWorker`` collects, and what it refuses when the worker starts.

A fresh agent is declared in each test that needs one: an agent is plain data, and the check
under test is what its worker makes of the declarations.
"""

import pytest
from pydantic import BaseModel, ConfigDict
from scripted_graphs import ScriptedGraph

from dsl import handlers
from dsl.definitions import Greet, Unlisted
from dsl.workflow import Greeter
from openmarketer_core.agent_runtime.agent import Agent
from openmarketer_worker.agent_runtime.agent_worker import AgentWorker, InvalidAgentWorker
from openmarketer_worker.agent_runtime.routes import ActionContext
from openmarketer_worker.checkpoint_cleanup import CheckpointCleanup


class Plain(BaseModel):
    """A model that no agent declared as an action."""

    model_config = ConfigDict(extra="forbid")


def an_agent_with_the_action_send() -> tuple[Agent, type[BaseModel]]:
    agent = Agent("dsl-fresh", state=dict)

    @agent.action("send")
    class Send(BaseModel):
        model_config = ConfigDict(extra="forbid")

    return agent, Send


# --------------------------------------------------------------- registering handlers
def test_handler_is_returned_as_it_is_so_that_it_is_tested_as_a_plain_function():
    result = handlers.greet(Greet(name="Ada"), ActionContext("dsl-greeter:1:2"))
    assert result == {"text": "hello, Ada"}
    assert handlers.service.calls == [(Greet(name="Ada"), "dsl-greeter:1:2")]


def test_handler_for_a_model_the_agent_did_not_declare_is_refused_naming_the_model():
    agent, _ = an_agent_with_the_action_send()
    with pytest.raises(InvalidAgentWorker, match="model Plain is not the payload of an action"):
        AgentWorker(agent, task_queue="q").activity(Plain)


def test_handler_for_a_model_another_agent_declared_is_refused_naming_the_model():
    agent, _ = an_agent_with_the_action_send()
    with pytest.raises(InvalidAgentWorker, match="model Unlisted is not the payload"):
        AgentWorker(agent, task_queue="q").activity(Unlisted)


def test_second_handler_for_one_action_is_refused_naming_the_action_and_both_handlers():
    agent, send = an_agent_with_the_action_send()
    worker = AgentWorker(agent, task_queue="q")

    @worker.activity(send)
    def first(payload, context):
        return {}

    with pytest.raises(InvalidAgentWorker, match="'send'.*two handlers.*first.*second"):

        @worker.activity(send)
        def second(payload, context):
            return {}


def test_handler_that_is_async_is_refused_because_a_handler_blocks_in_a_thread():
    agent, send = an_agent_with_the_action_send()
    with pytest.raises(InvalidAgentWorker, match="'send' is an async def"):

        @AgentWorker(agent, task_queue="q").activity(send)  # pyright: ignore[reportArgumentType]
        async def handler(payload, context):
            return {}


# --------------------------------------------------------------------- registration
def test_action_with_no_handler_is_refused_when_the_worker_starts_naming_the_action(checkpoints):
    agent, _ = an_agent_with_the_action_send()
    worker = AgentWorker(agent, task_queue="q")
    with pytest.raises(InvalidAgentWorker, match="'send' with no handler"):
        worker.registration(checkpoints=checkpoints, graph=ScriptedGraph([]), workflows=[Greeter])


def test_workflow_that_is_not_a_temporal_workflow_is_refused_naming_the_class(checkpoints):
    agent, send = an_agent_with_the_action_send()
    worker = AgentWorker(agent, task_queue="q")
    worker.activity(send)(lambda payload, context: {})
    with pytest.raises(InvalidAgentWorker, match="Plain is not a Temporal workflow"):
        worker.registration(checkpoints=checkpoints, graph=ScriptedGraph([]), workflows=[Plain])


def test_registration_holds_the_queue_the_workflows_and_every_activity_the_run_needs(checkpoints):
    cleanup = CheckpointCleanup(runs="greetings", remove=lambda session: 0)
    registration = handlers.worker.registration(
        checkpoints=checkpoints,
        graph=ScriptedGraph([]),
        workflows=[Greeter],
        activities=[handlers.note],
        checkpoint_cleanups=[cleanup],
    )
    assert registration.task_queue == handlers.TASK_QUEUE
    assert registration.workflows == [Greeter]
    assert [activity.__name__ for activity in registration.activities] == [
        "run_graph_step",
        "run_action",
        "forget_run",
        "note",
    ]
    assert registration.checkpoint_cleanups == [cleanup]
