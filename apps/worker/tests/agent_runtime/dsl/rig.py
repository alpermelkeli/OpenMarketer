"""Starting the greeter's workflows on a real Temporal server, and watching what they forget.

``Starter`` starts a workflow the way the API does, by its name, and reads what became of it.
``Forgetting`` stands between the workflows and the real checkpoints: it notes, each time a run is
forgotten, whether the run still had checkpoints, so that a test can tell a run whose checkpoints
were forgotten from one that never had any.
"""

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle, WorkflowHistory

from dsl.definitions import KIND
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.agent_runtime.threads import thread_id
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess


def some_run() -> GraphRunRef:
    return GraphRunRef(kind=KIND, run_id=uuid.uuid4())


def thread_of(run: GraphRunRef) -> str:
    return thread_id(run.kind, run.run_id)


@dataclass
class Starter:
    client: Client
    task_queue: str

    async def start(self, workflow_name: str, run: GraphRunRef) -> WorkflowHandle:
        return await self.client.start_workflow(
            workflow_name, run, id=f"{workflow_name}-{run.run_id}", task_queue=self.task_queue
        )

    async def finish(self, workflow_name: str, run: GraphRunRef) -> dict[str, Any]:
        handle = await self.start(workflow_name, run)
        # A workflow task that cannot be decoded is tried again for ever: a deadline makes a run
        # that hangs a failure of the test.
        return await asyncio.wait_for(handle.result(), timeout=30)

    async def fail(self, workflow_name: str, run: GraphRunRef) -> BaseException:
        """What the run failed with: the failure that ended the workflow."""
        handle = await self.start(workflow_name, run)
        with pytest.raises(WorkflowFailureError) as failed:
            await asyncio.wait_for(handle.result(), timeout=30)
        assert failed.value.__cause__ is not None
        return failed.value.__cause__

    async def history(self, workflow_name: str, run: GraphRunRef) -> WorkflowHistory:
        return await self.client.get_workflow_handle(
            f"{workflow_name}-{run.run_id}"
        ).fetch_history()


@dataclass
class Forgetting:
    """The ``forget`` of the checkpoints the workflows reach: it asks the real one, and notes it.

    ``asked`` holds, for each run forgotten, its thread and whether it had checkpoints when it
    was asked to forget them. ``fails_with`` makes the forgetting fail, as an activity does that
    words its failure.
    """

    checkpoints: CheckpointAccess
    holds_checkpoints: Callable[[str], bool]  # whether the test database holds the thread's
    asked: list[tuple[str, bool]] = field(default_factory=list)
    fails_with: Exception | None = None

    def __call__(self, thread: str) -> None:
        self.asked.append((thread, self.holds_checkpoints(thread)))
        if self.fails_with is not None:
            raise self.fails_with
        self.checkpoints.forget(thread)
