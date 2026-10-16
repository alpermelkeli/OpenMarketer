"""Fixtures of the greeter's tests: a worker that serves it, and a fresh outside world for each.

Workflows are tested against the dev stack's Temporal server and the real checkpoints in
PostgreSQL, as the folder above does, and are skipped when they are not reachable. The child
workflow the greeter's run actions start is served by a second worker, on the queue the
targets name (``dsl.definitions.CHILD_TASK_QUEUE``), as a real child of another agent is
served by that agent's worker.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import pytest
from temporalio.worker import Worker

from dsl.child import GreetingChild
from dsl.definitions import CHILD_TASK_QUEUE
from dsl.rig import Forgetting, Starter
from dsl.wiring import GreeterWorld, wire
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess

Serving = Callable[[], AbstractAsyncContextManager[Starter]]


@pytest.fixture
def world() -> GreeterWorld:
    """The outside world of one test: a service that does as the test says, and nothing noted."""
    return GreeterWorld()


@pytest.fixture
def forgetting(checkpoints, database) -> Forgetting:
    return Forgetting(checkpoints, database.thread_has_checkpoints)


@pytest.fixture
def serving(temporal, task_queue, checkpoints, forgetting, world) -> Serving:
    """A worker serving the greeter's registration on the test's own queue, and the child's."""
    forgetful = CheckpointAccess(of_attempt=checkpoints.of_attempt, forget=forgetting)

    @asynccontextmanager
    async def serve() -> AsyncIterator[Starter]:
        registration = wire(world, forgetful)
        async with Worker(
            temporal,
            task_queue=task_queue,
            workflows=registration.workflows,
            activities=registration.activities,
        ):
            async with Worker(temporal, task_queue=CHILD_TASK_QUEUE, workflows=[GreetingChild]):
                yield Starter(temporal, task_queue)

    return serve
