"""Tests for the Temporal adapters that start analysis workflows and tell runs of decisions.

Those that start a workflow use the dev stack's Temporal server (``make up``)
and are skipped when it is not reachable, the way the worker's tests are. Each
uses a task queue of its own, so no worker picks the workflow up, and ends the
workflow it started.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from temporalio.client import Client, WorkflowExecutionStatus

from openmarketer_api.temporal_workflows import (
    TemporalAnalysisWorkflows,
    TemporalConnection,
    TemporalRunSignals,
)
from openmarketer_core.agent_runtime.gate import PROPOSAL_DECIDED_SIGNAL
from openmarketer_core.approvals.decision import RunNotTold
from openmarketer_core.repository_analysis.request import WorkflowNotStarted
from openmarketer_core.repository_analysis.workflow_contract import (
    ANALYZE_REPOSITORY_WORKFLOW,
    AnalyzeRepositoryInput,
    analysis_workflow_id,
)
from openmarketer_core.workflow_server import WorkflowServer

DEV_STACK = WorkflowServer.from_environment(os.environ)
# Port 1 is reserved and nothing listens on it.
NOWHERE = WorkflowServer(address="127.0.0.1:1")


def some_run() -> AnalyzeRepositoryInput:
    return AnalyzeRepositoryInput(
        workspace_id=uuid.uuid4(), project_id=uuid.uuid4(), run_id=uuid.uuid4()
    )


@pytest.fixture
def task_queue() -> str:
    return f"test-{uuid.uuid4()}"


@pytest.fixture
async def temporal() -> AsyncIterator[Client]:
    try:
        client = await asyncio.wait_for(
            Client.connect(DEV_STACK.address, namespace=DEV_STACK.namespace), timeout=5
        )
    except (RuntimeError, TimeoutError):
        pytest.skip("Temporal is not reachable (run `make up`)")
    yield client


@pytest.fixture
async def started(temporal, task_queue) -> AsyncIterator[AnalyzeRepositoryInput]:
    """A run whose workflow the adapter started; the workflow is ended after the test."""
    run = some_run()
    await TemporalAnalysisWorkflows(TemporalConnection(DEV_STACK), task_queue=task_queue).start(run)
    yield run
    await temporal.get_workflow_handle(analysis_workflow_id(run.run_id)).terminate()


async def test_started_workflow_exists_under_the_id_derived_from_the_run(
    temporal, started, task_queue
):
    workflow = await temporal.get_workflow_handle(analysis_workflow_id(started.run_id)).describe()
    assert (workflow.workflow_type, workflow.task_queue, workflow.status) == (
        ANALYZE_REPOSITORY_WORKFLOW,
        task_queue,
        WorkflowExecutionStatus.RUNNING,
    )


async def test_workflow_has_no_time_limit_of_its_own(temporal, started):
    workflow = await temporal.get_workflow_handle(analysis_workflow_id(started.run_id)).describe()
    config = workflow.raw_description.execution_config
    assert config.workflow_execution_timeout.ToSeconds() == 0
    assert config.workflow_run_timeout.ToSeconds() == 0


async def test_starting_a_run_that_is_already_started_is_not_an_error(started, task_queue):
    await TemporalAnalysisWorkflows(TemporalConnection(DEV_STACK), task_queue=task_queue).start(
        started
    )


async def test_unreachable_server_is_reported_as_not_started():
    workflows = TemporalAnalysisWorkflows(TemporalConnection(NOWHERE), task_queue="unused")
    with pytest.raises(WorkflowNotStarted, match="not reachable at 127.0.0.1:1"):
        await workflows.start(some_run())


async def test_signal_reaches_the_workflow_of_the_run_with_the_proposal_id(temporal, started):
    proposal_id = uuid.uuid4()
    workflow_id = analysis_workflow_id(started.run_id)

    await TemporalRunSignals(TemporalConnection(DEV_STACK)).tell_decided(workflow_id, proposal_id)

    history = await temporal.get_workflow_handle(workflow_id).fetch_history()
    signals = [
        event.workflow_execution_signaled_event_attributes
        for event in history.events
        if event.HasField("workflow_execution_signaled_event_attributes")
    ]
    assert [signal.signal_name for signal in signals] == [PROPOSAL_DECIDED_SIGNAL]
    assert str(proposal_id) in signals[0].input.payloads[0].data.decode()


async def test_signal_to_a_workflow_that_does_not_exist_is_reported_as_not_told(temporal):
    signals = TemporalRunSignals(TemporalConnection(DEV_STACK))
    with pytest.raises(RunNotTold, match="Temporal refused"):
        await signals.tell_decided(f"no-such-workflow-{uuid.uuid4()}", uuid.uuid4())


async def test_signal_to_an_unreachable_server_is_reported_as_not_told():
    signals = TemporalRunSignals(TemporalConnection(NOWHERE))
    with pytest.raises(RunNotTold, match="not reachable at 127.0.0.1:1"):
        await signals.tell_decided("unused", uuid.uuid4())
