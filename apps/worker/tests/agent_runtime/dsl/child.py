"""The small workflow the greeter's run actions start, served on its own task queue.

It is a plain Temporal workflow and not an agent: what the runtime starts is the workflow a
target names, and the child keeps its own record, its own actions and its own approvals. Its
input's ``outcome`` says what it does: greet at once; fail at once (so a test can watch a
parent being told of a child that did not finish); sleep before greeting (so a test can
watch it finish on its own when its parent is cancelled); or wait until it is cancelled (for
the test that the run a waiting parent is cancelled with it).

The greeter's test worker serves it on ``CHILD_TASK_QUEUE``, a queue of its own that only the
target names: a test reads the child's start from the parent's history and sees the queue
the target declared, and a worker on another queue could not have taken it.
"""

from typing import Any

from temporalio import workflow
from temporalio.exceptions import ApplicationError

from dsl.definitions import (
    CHILD_FAILS,
    CHILD_GREETS_SLOWLY,
    CHILD_WAITS,
    CHILD_WORKFLOW,
    ChildRequest,
)

# What the child fails with: a type and message of its own, which stay in its own record.
CHILD_REFUSED = "ChildRefused"
# Long enough that a test cancels the parent while the child still works.
SLEEP_SECONDS = 2


@workflow.defn(name=CHILD_WORKFLOW)
class GreetingChild:
    @workflow.run
    async def run(self, request: ChildRequest) -> dict[str, Any]:
        if request.outcome == CHILD_FAILS:
            raise ApplicationError(
                "the child could not greet", type=CHILD_REFUSED, non_retryable=True
            )
        if request.outcome == CHILD_WAITS:
            await workflow.wait_condition(lambda: False)
        elif request.outcome == CHILD_GREETS_SLOWLY:
            await workflow.sleep(SLEEP_SECONDS)
        return {"greeting": f"hello, {request.name}", "child_run_id": str(request.run_id)}
