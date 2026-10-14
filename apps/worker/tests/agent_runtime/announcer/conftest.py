"""Fixtures of the announcer's tests: a worker that serves it, a project, and how a person decides.

The workflow runs on the dev stack's Temporal server and stores its proposals in the test's
PostgreSQL database; both are skipped when they are not reachable, as everywhere in the worker.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

import pytest
from sqlalchemy.orm import Session, sessionmaker
from temporalio.client import Client, WorkflowHandle
from temporalio.worker import Worker

from announcer.definitions import AnnouncerRun
from announcer.wiring import Board, wire
from openmarketer_core.agent_runtime.gate import PROPOSAL_DECIDED_SIGNAL, RunScope
from openmarketer_core.db.models import Project
from openmarketer_core.db.proposals import ActionProposal, decide_proposal, list_proposals
from openmarketer_core.db.session import transaction

PERSON = uuid.uuid4()


@dataclass
class Desk:
    """Where the tests start runs and decide proposals, as the API would."""

    client: Client
    task_queue: str
    sessions: sessionmaker[Session]
    scope: RunScope

    async def start(self, run_id: uuid.UUID | None = None) -> tuple[AnnouncerRun, WorkflowHandle]:
        run = AnnouncerRun(
            workspace_id=self.scope.workspace_id,
            project_id=self.scope.project_id,
            run_id=run_id or uuid.uuid4(),
        )
        handle = await self.client.start_workflow(
            "Announcer", run, id=f"announcer-{run.run_id}", task_queue=self.task_queue
        )
        return run, handle

    def proposals(self) -> tuple[ActionProposal, ...]:
        with transaction(self.sessions) as session:
            return list_proposals(
                session,
                workspace_id=self.scope.workspace_id,
                project_id=self.scope.project_id,
                limit=10,
            ).items

    async def pending_proposal(self, *, within: float = 20) -> ActionProposal:
        """The run's proposal, once the run has stored it."""

        async def stored() -> ActionProposal:
            while not (found := self.proposals()):
                await asyncio.sleep(0.1)
            return found[0]

        return await asyncio.wait_for(stored(), timeout=within)

    async def decide(
        self, handle: WorkflowHandle, proposal: ActionProposal, *, approved: bool
    ) -> None:
        """Record the decision, then tell the run: what the API does."""
        with transaction(self.sessions) as session:
            decide_proposal(
                session,
                workspace_id=self.scope.workspace_id,
                project_id=self.scope.project_id,
                proposal_id=proposal.id,
                approved=approved,
                decided_by=PERSON,
            )
        await handle.signal(PROPOSAL_DECIDED_SIGNAL, proposal.id)


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
