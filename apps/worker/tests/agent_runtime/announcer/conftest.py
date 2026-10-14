"""Fixtures of the announcer's tests: a worker that serves it, a project, and how a person decides.

The workflow runs on the dev stack's Temporal server and stores its proposals in the test's
PostgreSQL database; both are skipped when they are not reachable, as everywhere in the worker.
"""

from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import pytest
from temporalio.worker import Worker

from announcer.rig import Desk
from announcer.wiring import Board, wire
from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.db.models import Project
from openmarketer_core.db.session import transaction


@pytest.fixture
def board() -> Board:
    return Board()


@pytest.fixture
def scope(database, sessions) -> RunScope:
    workspace_id = database.new_workspace()
    with transaction(sessions) as session:
        project = Project(
            workspace_id=workspace_id, name="Example", source_repo_url="https://example.com/r.git"
        )
        session.add(project)
        session.flush()
        return RunScope(workspace_id=workspace_id, project_id=project.id)


@pytest.fixture
def serving(
    temporal, task_queue, checkpoints, sessions, board, scope
) -> Callable[[], AbstractAsyncContextManager[Desk]]:
    @asynccontextmanager
    async def serve() -> AsyncIterator[Desk]:
        registration = wire(board, checkpoints, sessions)
        async with Worker(
            temporal,
            task_queue=task_queue,
            workflows=registration.workflows,
            activities=registration.activities,
            # The gate's activities are blocking database writes, as in the worker's process.
            activity_executor=ThreadPoolExecutor(max_workers=4),
        ):
            yield Desk(temporal, task_queue, sessions, scope)

    return serve
