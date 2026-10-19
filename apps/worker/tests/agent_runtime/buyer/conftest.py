"""Fixtures of the buyer's tests: a studio, a project, and a worker that serves the buyer.

The workflow runs on the dev stack's Temporal server and keeps its ledger and proposals in the
test's PostgreSQL database; both are skipped when they are not reachable, as everywhere in the
worker.
"""

from collections.abc import AsyncIterator, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import pytest
from temporalio.worker import Worker

from buyer.rig import Desk
from buyer.wiring import CAPS_IN_THE_ENVIRONMENT, Studio, wire
from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.db.models import Project
from openmarketer_core.db.session import transaction

Serving = Callable[..., AbstractAsyncContextManager[Desk]]


@pytest.fixture
def studio() -> Studio:
    return Studio()


@pytest.fixture
def scope(database, sessions) -> RunScope:
    """A project in a workspace of its own, so that no other test's spending counts against it."""
    workspace_id = database.new_workspace()
    with transaction(sessions) as session:
        project = Project(
            workspace_id=workspace_id, name="Example", source_repo_url="https://example.com/r.git"
        )
        session.add(project)
        session.flush()
        return RunScope(workspace_id=workspace_id, project_id=project.id)


@pytest.fixture
def serving(temporal, task_queue, checkpoints, sessions, studio, scope) -> Serving:
    @asynccontextmanager
    async def serve(
        environ: Mapping[str, str] = CAPS_IN_THE_ENVIRONMENT,
        replace: Callable[[list], list] | None = None,
    ) -> AsyncIterator[Desk]:
        """Serve the buyer; ``replace`` may swap some of its activities for a test's own."""
        registration = wire(studio, checkpoints, sessions, environ)
        activities = list(registration.activities)
        async with Worker(
            temporal,
            task_queue=task_queue,
            workflows=registration.workflows,
            activities=activities if replace is None else replace(activities),
            # The gate's and the budget's activities are blocking database writes, as in the
            # worker's process.
            activity_executor=ThreadPoolExecutor(max_workers=4),
        ):
            yield Desk(temporal, task_queue, sessions, scope)

    return serve
