"""Fixtures of the greeter's tests: a worker that serves it, and a fresh outside world for each.

Workflows are tested against the dev stack's Temporal server and the real checkpoints in
PostgreSQL, as the folder above does, and are skipped when they are not reachable.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import pytest
from temporalio.worker import Worker

from dsl import graph, handlers
from dsl.rig import Forgetting, Starter
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess

Serving = Callable[[], AbstractAsyncContextManager[Starter]]


@pytest.fixture(autouse=True)
def a_fresh_outside_world(monkeypatch):
    """No call, note or subject of an earlier test, and a service that does as the test says."""
    monkeypatch.setattr(handlers, "service", handlers.GreetingService())
    handlers.notes.clear()
    graph.subjects.clear()


@pytest.fixture
def forgetting(checkpoints, database) -> Forgetting:
    return Forgetting(checkpoints, database.thread_has_checkpoints)


@pytest.fixture
def serving(temporal, task_queue, checkpoints, forgetting) -> Serving:
    """A worker serving the greeter's registration on the test's own queue."""
    forgetful = CheckpointAccess(of_attempt=checkpoints.of_attempt, forget=forgetting)

    @asynccontextmanager
    async def serve() -> AsyncIterator[Starter]:
        registration = handlers.registration(forgetful)
        async with Worker(
            temporal,
            task_queue=task_queue,
            workflows=registration.workflows,
            activities=registration.activities,
        ):
            yield Starter(temporal, task_queue)

    return serve
