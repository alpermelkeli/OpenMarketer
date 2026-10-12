"""Running the agent runtime's test workflows against a real Temporal server.

``serving_runs`` serves the runtime's activities and the test workflows on a task queue of the
test's own, and hands out an ``Execution`` to start runs on it and read what became of them.
The limits are short enough for a test and long enough for a slow machine; the dev server
waits at least a second before a retry, whatever interval was asked for.
"""

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import empty_checkpoint
from scripted_agent import (
    RecordsEveryFailure,
    RecordsLikeTheAnalyzer,
    ScriptedAgent,
    ScriptedRun,
    WritesToTheDatabase,
)
from scripted_passthrough_agent import PassedThroughRun, PassesTheDriverThrough
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle, WorkflowHistory
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.worker import Worker

from openmarketer_core.agent_runtime.actions import ActionPolicy
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.agent_runtime.threads import thread_id
from openmarketer_worker.agent_runtime.activities import RuntimeActivities
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess
from openmarketer_worker.agent_runtime.vocabulary import WORDED_BY_THE_RUNTIME, GraphRunPolicy

KIND = "scripted-agent"
STEP_LIMITS = GraphRunPolicy(
    attempt_timeout_seconds=30,
    heartbeat_timeout_seconds=15,
    max_attempts=2,
    retry_after_seconds=0.2,
)
ACTION_LIMITS = ActionPolicy(attempt_timeout_seconds=30, max_attempts=2, retry_after_seconds=0.2)
ALLOWED = {"send_email": ACTION_LIMITS, "publish": ACTION_LIMITS}
# The type the scripted handlers word their failures with, and the types the scripted agent
# declares: the runtime's, and that one.
REFUSED_BY_A_HANDLER = "Refused"
WORDED = [*WORDED_BY_THE_RUNTIME, REFUSED_BY_A_HANDLER]


def some_run() -> GraphRunRef:
    return GraphRunRef(kind=KIND, run_id=uuid.uuid4())


def thread_of(run: GraphRunRef) -> str:
    return thread_id(run.kind, run.run_id)


def a_thread_with_a_checkpoint(checkpoints: CheckpointAccess, thread: str) -> None:
    """Gives ``thread`` one checkpoint, as the thread of a run that has taken a step has."""
    with checkpoints.of_attempt(thread) as held:
        config: RunnableConfig = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
        held.store.put(config, empty_checkpoint(), {"source": "loop", "step": 1}, {})


def cause_of(failure: BaseException) -> ApplicationError:
    """The error an activity ended with, under the failure of the workflow."""
    assert isinstance(failure, ActivityError)
    assert isinstance(failure.cause, ApplicationError)
    return failure.cause


@dataclass
class Execution:
    client: Client
    task_queue: str

    async def start(
        self,
        run: GraphRunRef,
        *,
        actions: dict[str, ActionPolicy] | None = None,
        policy: GraphRunPolicy = STEP_LIMITS,
        worded_types: list[str] = WORDED,
        workflow_run: Any = ScriptedAgent.run,
    ) -> WorkflowHandle:
        # The limits are the input of this workflow so that each test sets its own. An agent's
        # workflow keeps them as constants: a limit that fails validation fails decoding the input.
        scripted = ScriptedRun(
            run=run,
            actions=ALLOWED if actions is None else actions,
            policy=policy,
            worded_types=worded_types,
        )
        return await self.client.start_workflow(
            workflow_run, scripted, id=f"scripted-{run.run_id}", task_queue=self.task_queue
        )

    async def finish(self, run: GraphRunRef, **limits: Any) -> dict[str, Any]:
        return await (await self.start(run, **limits)).result()

    async def finish_with_the_driver_passed_through(self, run: GraphRunRef) -> dict[str, Any]:
        """The same run, started on the workflow that imports the driver through the sandbox."""
        scripted = PassedThroughRun(run=run, actions=ALLOWED, policy=STEP_LIMITS)
        handle = await self.client.start_workflow(
            PassesTheDriverThrough.run,
            scripted,
            id=f"passed-through-{run.run_id}",
            task_queue=self.task_queue,
        )
        # A workflow task that cannot decode a result fails and is tried again for ever, so the
        # run would hang: a deadline turns that into a failure of the test.
        return await asyncio.wait_for(handle.result(), timeout=20)

    async def fail(self, run: GraphRunRef, **limits: Any) -> BaseException:
        """What the run failed with: the failure that ended the workflow."""
        with pytest.raises(WorkflowFailureError) as failed:
            await self.finish(run, **limits)
        assert failed.value.__cause__ is not None
        return failed.value.__cause__

    async def history(self, run: GraphRunRef) -> WorkflowHistory:
        return await self.client.get_workflow_handle(f"scripted-{run.run_id}").fetch_history()


@asynccontextmanager
async def serving_runs(
    client: Client, task_queue: str, activities: RuntimeActivities
) -> AsyncIterator[Execution]:
    """A worker with the runtime's activities and the workflows of the tests."""
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[
            ScriptedAgent,
            RecordsLikeTheAnalyzer,
            RecordsEveryFailure,
            PassesTheDriverThrough,
            WritesToTheDatabase,
        ],
        activities=[activities.run_graph_step, activities.run_action],
    )
    async with worker:
        yield Execution(client, task_queue)


async def happened(event: threading.Event, *, within: float = 20) -> None:
    """Returns once a thread has set ``event``; fails the test when it does not in time."""
    reached = await asyncio.get_running_loop().run_in_executor(None, event.wait, within)
    assert reached, "the action did not start"


async def failed_an_attempt_with(
    handle: WorkflowHandle, failure_type: str, *, within: float = 20
) -> None:
    """Returns once the activity the workflow waits for has failed an attempt of that type.

    Failed attempts that are tried again are not in history, but the server keeps the last one
    of the activity that is pending.
    """

    async def polled() -> None:
        while True:
            description = await handle.describe()
            for pending in description.raw_description.pending_activities:
                if pending.last_failure.application_failure_info.type == failure_type:
                    return
            await asyncio.sleep(0.2)

    await asyncio.wait_for(polled(), timeout=within)
