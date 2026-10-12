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

from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_worker.agent_runtime.activities import AgentGraph, RuntimeActivities
from openmarketer_worker.agent_runtime.routes import ActionRoutes
from openmarketer_worker.agent_runtime.vocabulary import RUN_TEXTS, FailureTexts

# The workflow sandbox imports the module of a workflow by name, and the test folders are not
# packages: this makes ``scripted_agent`` importable the way an agent's workflow module is.
sys.path.insert(0, str(Path(__file__).parent))

from scripted_rig import KIND, Execution, serving_runs  # noqa: E402

Serving = Callable[[RuntimeActivities], AbstractAsyncContextManager[Execution]]


@pytest.fixture
def runtime(checkpoints) -> Callable[..., RuntimeActivities]:
    """The runtime's activities for a graph and routes, on the test database.

    They are the scripted agent's (``KIND``) unless a test gives another kind, in the runtime's
    own words unless it gives others, and tell ``on_run_end`` of the runs they forget, if it
    gives one.
    """

    def activities(
        graph: AgentGraph,
        routes: ActionRoutes,
        *,
        kind: str = KIND,
        texts: FailureTexts = RUN_TEXTS,
        on_run_end: Callable[[GraphRunRef], None] | None = None,
    ) -> RuntimeActivities:
        return RuntimeActivities(
            checkpoints, graph, routes, kind=kind, texts=texts, on_run_end=on_run_end
        )

    return activities


@pytest.fixture
def serving(temporal, task_queue) -> Serving:
    """A worker serving the runtime's activities and the test workflows on the test's own queue."""

    def worker(activities: RuntimeActivities) -> AbstractAsyncContextManager[Execution]:
        return serving_runs(temporal, task_queue, activities)

    return worker
