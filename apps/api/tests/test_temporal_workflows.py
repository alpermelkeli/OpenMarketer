"""Tests for the Temporal adapter that starts the workflows of stored runs.

Those that start a workflow use the dev stack's Temporal server (``make up``)
and are skipped when it is not reachable, the way the worker's tests are. Each
uses a task queue of its own, so no worker picks the workflow up, and ends the
workflow it started.
"""

import asyncio
import dataclasses
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from temporalio.client import Client, WorkflowExecutionStatus

from openmarketer_api.temporal_workflows import TemporalWorkflows
from openmarketer_core.repository_analysis.workflow_contract import (
    AnalyzeRepositoryInput,
    analysis_workflow,
)
from openmarketer_core.workflow_server import WorkflowServer
from openmarketer_core.workflow_start import WorkflowNotStarted, WorkflowStart

DEV_STACK = WorkflowServer.from_environment(os.environ)
# Port 1 is reserved and nothing listens on it.
NOWHERE = WorkflowServer(address="127.0.0.1:1")


@dataclasses.dataclass(frozen=True)
class OtherRunInput:
    """The input of a kind of run that is not an analysis, as another contract would declare it."""

    run_id: uuid.UUID


def analysis_on(task_queue: str) -> WorkflowStart:
    """The workflow of a new analysis run, as its contract describes it, on the test's queue."""
    run = AnalyzeRepositoryInput(
        workspace_id=uuid.uuid4(), project_id=uuid.uuid4(), run_id=uuid.uuid4()
    )
    return dataclasses.replace(analysis_workflow(run), task_queue=task_queue)


def other_run_on(task_queue: str) -> WorkflowStart:
    run_id = uuid.uuid4()
    return WorkflowStart(
        workflow="TestOtherRun",
        workflow_id=f"test-other-run-{run_id}",
        task_queue=task_queue,
        input=OtherRunInput(run_id=run_id),
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
async def ending(temporal) -> AsyncIterator[list[WorkflowStart]]:
    """The workflows a test started, ended after it."""
    started: list[WorkflowStart] = []
    yield started
    for workflow in started:
        await temporal.get_workflow_handle(workflow.workflow_id).terminate()


@pytest.fixture
async def started(task_queue, ending) -> WorkflowStart:
    """An analysis whose workflow the adapter started."""
    workflow = analysis_on(task_queue)
    await TemporalWorkflows(DEV_STACK).start(workflow)
    ending.append(workflow)
    return workflow


async def test_started_workflow_runs_under_the_name_id_and_queue_it_was_given(temporal, started):
    workflow = await temporal.get_workflow_handle(started.workflow_id).describe()
    assert (workflow.workflow_type, workflow.task_queue, workflow.status) == (
        started.workflow,
        started.task_queue,
        WorkflowExecutionStatus.RUNNING,
    )


async def test_workflows_of_two_kinds_of_run_are_started_by_one_adapter(
    temporal, task_queue, ending
):
    workflows = TemporalWorkflows(DEV_STACK)
    kinds = [analysis_on(task_queue), other_run_on(f"{task_queue}-other")]
    for workflow in kinds:
        await workflows.start(workflow)
        ending.append(workflow)
    described = [await temporal.get_workflow_handle(w.workflow_id).describe() for w in kinds]
    assert [(d.workflow_type, d.task_queue) for d in described] == [
        (w.workflow, w.task_queue) for w in kinds
    ]


async def test_workflow_has_no_time_limit_of_its_own(temporal, started):
    workflow = await temporal.get_workflow_handle(started.workflow_id).describe()
    config = workflow.raw_description.execution_config
    assert config.workflow_execution_timeout.ToSeconds() == 0
    assert config.workflow_run_timeout.ToSeconds() == 0


async def test_starting_a_workflow_that_is_already_started_is_not_an_error(started):
    await TemporalWorkflows(DEV_STACK).start(started)


async def test_unreachable_server_is_reported_as_not_started():
    with pytest.raises(WorkflowNotStarted, match="not reachable at 127.0.0.1:1"):
        await TemporalWorkflows(NOWHERE).start(analysis_on("unused"))
