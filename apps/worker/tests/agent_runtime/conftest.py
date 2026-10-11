"""Fixtures of the agent runtime's tests: its activities, and a worker that serves them.

Activities are tested against PostgreSQL, through the ``checkpoints`` of the worker's
``conftest.py`` one folder up, and workflows against the dev stack's Temporal server; both
are skipped when they are not reachable.
"""

import sys
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path

import pytest

from openmarketer_worker.agent_runtime.activities import AgentGraph, RuntimeActivities
from openmarketer_worker.agent_runtime.routes import ActionRoutes
from openmarketer_worker.agent_runtime.vocabulary import GraphRunPolicy

# The workflow sandbox imports the module of a workflow by name, and the test folders are not
# packages: this makes ``scripted_agent`` importable the way an agent's workflow module is.
sys.path.insert(0, str(Path(__file__).parent))

from scripted_work import STEP_LIMITS, Execution, serving_runs  # noqa: E402

Serving = Callable[[RuntimeActivities], AbstractAsyncContextManager[Execution]]


@pytest.fixture
def runtime(checkpoints) -> Callable[..., RuntimeActivities]:
    """The runtime's activities for a graph and routes, on the test database."""

    def activities(
        graph: AgentGraph, routes: ActionRoutes, policy: GraphRunPolicy = STEP_LIMITS
    ) -> RuntimeActivities:
        return RuntimeActivities(checkpoints, graph, routes, policy)

    return activities


@pytest.fixture
def serving(temporal, task_queue) -> Serving:
    """A worker serving the runtime's activities and the test workflows on the test's own queue."""

    def worker(activities: RuntimeActivities) -> AbstractAsyncContextManager[Execution]:
        return serving_runs(temporal, task_queue, activities)

    return worker
