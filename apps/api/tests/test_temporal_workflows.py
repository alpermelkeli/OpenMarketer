"""Tests for the Temporal adapter that starts analysis workflows.

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

from openmarketer_api.temporal_workflows import TemporalAnalysisWorkflows
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
    await TemporalAnalysisWorkflows(DEV_STACK, task_queue=task_queue).start(run)
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
    await TemporalAnalysisWorkflows(DEV_STACK, task_queue=task_queue).start(started)


async def test_unreachable_server_is_reported_as_not_started():
    workflows = TemporalAnalysisWorkflows(NOWHERE, task_queue="unused")
    with pytest.raises(WorkflowNotStarted, match="not reachable at 127.0.0.1:1"):
        await workflows.start(some_run())
