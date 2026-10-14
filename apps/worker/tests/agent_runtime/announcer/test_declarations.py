"""An agent whose actions wait for a person is refused, when it is declared, without what it needs.

Its workflow needs a scope for its proposals, and its registration the database to store them in.
"""

import pytest

from announcer.definitions import AnnouncerRun, agent, run_of
from announcer.wiring import wire
from openmarketer_worker.agent_runtime.agent_worker import InvalidAgentWorker
from openmarketer_worker.agent_runtime.vocabulary import GraphRunPolicy
from openmarketer_worker.agent_runtime.workflow_dsl import InvalidAgentWorkflow, agent_workflow

GRAPH = GraphRunPolicy(
    attempt_timeout_seconds=30, heartbeat_timeout_seconds=10, max_attempts=1, retry_after_seconds=1
)


def test_workflow_of_an_agent_whose_action_waits_for_a_person_needs_a_scope():
    with pytest.raises(InvalidAgentWorkflow, match="'announce' waits for a person's approval"):
        agent_workflow(agent, name="Unscoped", graph=GRAPH, input=AnnouncerRun, run_of=run_of)


def test_registration_of_an_agent_whose_action_waits_for_a_person_needs_the_database(
    board, checkpoints
):
    with pytest.raises(InvalidAgentWorker, match="'announce' of agent 'test-announcer' waits"):
        wire(board, checkpoints, sessions=None)  # pyright: ignore[reportArgumentType]
