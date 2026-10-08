"""Tests for what the API and the worker agree on about analysis workflows."""

import uuid

from openmarketer_core.analysis_workflow import WorkflowServer, analysis_workflow_id


def test_workflow_id_is_derived_from_the_run():
    run_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    assert analysis_workflow_id(run_id) == "analyze-repository-11111111-1111-4111-8111-111111111111"


def test_workflow_server_defaults_to_the_dev_stack_as_seen_from_the_host():
    assert WorkflowServer.from_environment({}) == WorkflowServer(
        address="localhost:7233", namespace="default"
    )


def test_workflow_server_is_read_from_the_environment():
    server = WorkflowServer.from_environment(
        {"TEMPORAL_ADDRESS": "temporal.internal:7233", "TEMPORAL_NAMESPACE": "marketing"}
    )
    assert server == WorkflowServer(address="temporal.internal:7233", namespace="marketing")


def test_empty_values_mean_the_defaults():
    """``.env`` files list the variables with nothing after the equals sign."""
    server = WorkflowServer.from_environment({"TEMPORAL_ADDRESS": "", "TEMPORAL_NAMESPACE": ""})
    assert server == WorkflowServer()
