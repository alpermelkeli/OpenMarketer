"""An agent whose action spends is refused, when the worker starts, without what the budget needs.

Every action that spends has an estimate; only such an action has one; and the registration is
given the database and the environment the caps are read from.
"""

from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from typing import Any, TypedDict

import pytest
from pydantic import BaseModel, ConfigDict

from buyer.definitions import BuyerRun, agent, run_of
from buyer.wiring import Studio, wire
from openmarketer_core.agent_runtime.actions import ActionEffect
from openmarketer_core.agent_runtime.agent import Agent
from openmarketer_worker.agent_runtime.agent_worker import AgentWorker, InvalidAgentWorker
from openmarketer_worker.agent_runtime.routes import ActionContext, SpendContext
from openmarketer_worker.agent_runtime.vocabulary import GraphRunPolicy
from openmarketer_worker.agent_runtime.workflow_dsl import InvalidAgentWorkflow, agent_workflow

GRAPH = GraphRunPolicy(
    attempt_timeout_seconds=30, heartbeat_timeout_seconds=10, max_attempts=1, retry_after_seconds=1
)


class Nothing(TypedDict):
    """The state of an agent whose graph is never run."""


# An agent of its own, with an action that spends and one that does not, and no graph: a
# registration is refused before a graph is needed.
unestimated = Agent("test-unestimated", state=Nothing)


class NoGraph:
    """Stands for the agent's graph, which a refused registration never reaches."""

    def advance(self, run, checkpoints, resume):
        raise AssertionError("no step is taken")


@unestimated.action("buy_ads", effect=ActionEffect.SPENDS)
class BuyAds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign: str


@unestimated.action("read_page")
class ReadPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: str


def handled(worker: AgentWorker) -> AgentWorker:
    @worker.handler(BuyAds)
    def buy_ads(payload: BuyAds, context: ActionContext) -> Mapping[str, Any]:
        return {}

    @worker.handler(ReadPage)
    def read_page(payload: ReadPage, context: ActionContext) -> Mapping[str, Any]:
        return {}

    return worker


def register(worker: AgentWorker, checkpoints, sessions, environ=None):
    return worker.registration(
        checkpoints=checkpoints,
        # Longer than the three days ``buy_ads`` may wait for a person.
        longest_run=timedelta(days=4),
        graph=NoGraph(),
        workflows=[],
        sessions=sessions,
        environ=environ,
    )


def test_action_that_spends_with_no_estimate_is_refused_when_the_worker_starts(
    checkpoints, sessions
):
    worker = handled(AgentWorker(unestimated, task_queue="test-unestimated"))

    with pytest.raises(InvalidAgentWorker, match="'buy_ads' that spend with no estimate"):
        register(worker, checkpoints, sessions, environ={})


def test_estimate_of_an_action_that_does_not_spend_is_refused():
    worker = AgentWorker(unestimated, task_queue="test-unestimated")

    with pytest.raises(InvalidAgentWorker, match="'read_page' of agent 'test-unestimated' does"):
        worker.estimate(ReadPage)


def test_second_estimate_of_one_action_is_refused():
    worker = AgentWorker(unestimated, task_queue="test-unestimated")
    worker.estimate(BuyAds)(lambda payload, context: Decimal(1))

    with pytest.raises(InvalidAgentWorker, match="two estimates"):
        worker.estimate(BuyAds)(lambda payload, context: Decimal(2))


def test_estimate_that_is_an_async_def_is_refused():
    worker = AgentWorker(unestimated, task_queue="test-unestimated")

    async def estimate(payload: BuyAds, context: SpendContext) -> Decimal:
        return Decimal(1)

    with pytest.raises(InvalidAgentWorker, match="async def"):
        worker.estimate(BuyAds)(estimate)  # pyright: ignore[reportArgumentType]


def test_registration_of_an_agent_that_spends_needs_the_environment(checkpoints, sessions):
    with pytest.raises(InvalidAgentWorker, match="'generate_image' of agent 'test-buyer' spends"):
        wire(Studio(), checkpoints, sessions, environ=None)  # pyright: ignore[reportArgumentType]


def test_registration_of_an_agent_that_spends_needs_the_database(checkpoints):
    with pytest.raises(InvalidAgentWorker, match="'generate_image' of agent 'test-buyer' spends"):
        wire(Studio(), checkpoints, sessions=None)  # pyright: ignore[reportArgumentType]


def test_workflow_of_an_agent_whose_action_spends_needs_a_scope():
    with pytest.raises(InvalidAgentWorkflow, match="'generate_image' waits for a person"):
        agent_workflow(agent, name="Unscoped", graph=GRAPH, input=BuyerRun, run_of=run_of)


def test_registration_of_the_buyer_registers_the_budgets_activities(checkpoints, sessions):
    names = {a.__name__ for a in wire(Studio(), checkpoints, sessions).activities}

    assert {"reserve_spend", "settle_spend", "propose_action", "settle_proposal"} <= names
