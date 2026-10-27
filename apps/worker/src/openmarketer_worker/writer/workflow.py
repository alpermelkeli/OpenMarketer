"""``WritePost``: one requested writing run, from picked up to finished.

The workflow is the writer's agent run by the agent runtime (``agent_workflow``): it takes the
steps of the agent's graph, runs the two actions the graph asks for as activities of their own
(``store_draft`` at once, ``publish_post`` only once a person approved it at the policy gate),
and forgets the run's checkpoints whatever happened. What is the writer's own is here, in three
hooks, and decides nothing about the post:

- ``before`` marks the run started and takes, from that activity, the limits this worker was
  configured with for the steps of the graph: workflow code cannot read the environment;
- ``after`` settles the run's post as the graph's result says it ended (recorded as a dry run,
  rejected or expired) and marks the run succeeded, both in one write. A post that a person
  approved and that could not be recorded, and a result that cannot be read, end the run as a
  failure instead, which ``failed`` records;
- ``failed`` records why on the run, with what its stored draft cost; a post the run proposed
  and nobody settled is settled ``failed`` with it. Every way a run can end without a settled
  post (no approved profile, a model's answers that cannot be read, attempts used up, a
  timeout, a worker that died, a cancellation, the post that could not be recorded) leads here.

The writer has no chat request action yet, so no run is followed up on in a chat: a run that a
chat asked for (``chat_thread_id``) ends like any other, and the chat is not told.

``scope_of`` names the run's workspace and project: the proposal of its post belongs there.
The history holds the run's identifiers, the limits, the content item's id, a status, an amount,
short messages and, for a failed activity, the stack trace Temporal records. The two actions'
payloads are the exception the agent's declarations bound: the draft and the post the writer's
model wrote, which the history of an action carries as its request. No brief, profile or
credential is in it.

The definitions are imported before any module of the worker: the agent's declarations must be
the sandbox's own copy, loaded first, so the agent the runtime's modules see is this one.
"""

import uuid
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ApplicationError

# One block, in which every module of core (the agent's declarations among them) comes before
# every module of the worker.
with workflow.unsafe.imports_passed_through():
    from openmarketer_core.agent_runtime.gate import RunScope
    from openmarketer_core.agent_runtime.steps import GraphRunRef
    from openmarketer_core.writing.workflow_contract import (
        WRITING_RUN_KIND,
        WRITING_WORKFLOW,
        WritingRunInput,
    )
    from openmarketer_core.writing.writer_agent.definitions import agent
    from openmarketer_core.writing.writer_agent.rules import PostStatus
    from openmarketer_worker.agent_runtime.vocabulary import GraphRunPolicy
    from openmarketer_worker.agent_runtime.workflow_dsl import agent_workflow
    from openmarketer_worker.agent_runtime.workflow_support import database_write, until_it_ends
    from openmarketer_worker.writer import policy, steps
    from openmarketer_worker.writer.policy import WriterPolicy

# How a post that a person decided on may end a run that succeeds.
SETTLED_BY_THE_RUN = (PostStatus.DRY_RUN, PostStatus.REJECTED, PostStatus.EXPIRED)


def run_of(run: WritingRunInput) -> GraphRunRef:
    """The run of the writer's graph that the workflow's input names."""
    return GraphRunRef(kind=WRITING_RUN_KIND, run_id=run.run_id)


def scope_of(run: WritingRunInput) -> RunScope:
    """The workspace and project whose proposals the run's post is held among."""
    return RunScope(workspace_id=run.workspace_id, project_id=run.project_id)


def graph_limits(writer: WriterPolicy) -> GraphRunPolicy:
    return GraphRunPolicy(
        attempt_timeout_seconds=writer.attempt_timeout_seconds,
        heartbeat_timeout_seconds=writer.heartbeat_timeout_seconds,
        max_attempts=writer.max_attempts,
        retry_after_seconds=writer.retry_after_seconds,
    )


@agent_workflow(
    agent,
    name=WRITING_WORKFLOW,
    input=WritingRunInput,
    run_of=run_of,
    scope_of=scope_of,
    # What a run is taken with when ``before`` gives nothing else, which it always does.
    graph=graph_limits(WriterPolicy()),
    texts=steps.WRITING_TEXTS,
    worded_types=steps.WORDED_BY_THE_ACTIVITY,
)
class WritePost:
    async def before(self, run: WritingRunInput) -> GraphRunPolicy:
        configured: WriterPolicy = await database_write(
            steps.START_RUN,
            run,
            policy.START_RUN,
            not_retried=steps.NOT_RETRIED,
            result_type=WriterPolicy,
        )
        return graph_limits(configured)

    async def after(self, run: WritingRunInput, output: dict[str, Any]) -> None:
        outcome = outcome_of(run, output)
        # The post is settled: a cancellation that comes now waits for the write, and does not
        # turn a run whose post a person decided on into one that failed.
        await until_it_ends(
            database_write(
                steps.RECORD_OUTCOME,
                outcome,
                policy.RECORD_OUTCOME,
                not_retried=steps.NOT_RETRIED,
            )
        )

    async def failed(self, run: WritingRunInput, reason: str) -> None:
        await database_write(
            steps.RECORD_FAILURE,
            steps.RunFailure(run=run, error=steps.in_the_writers_words(reason)),
            policy.RECORD_FAILURE,
            not_retried=steps.NOT_RETRIED,
        )


def outcome_of(run: WritingRunInput, output: dict[str, Any]) -> steps.RunOutcome:
    """What ``record_outcome`` settles for a run whose graph finished with ``output``.

    Raises an ``ApplicationError`` the run ends with when the approved post could not be
    recorded, and when the output is not what the wiring's ``finish`` reports: either way the
    run has no settled post, and ``failed`` records why.
    """
    status = output.get("status")
    if status == PostStatus.FAILED.value:
        error = output.get("error")
        detail = steps.in_the_writers_words(error) if isinstance(error, str) and error else ""
        raise ApplicationError(
            f"{steps.POST_NOT_RECORDED}: {detail}" if detail else steps.POST_NOT_RECORDED,
            type=steps.WRITING_FAILED,
            non_retryable=True,
        )
    item = output.get("content_item_id")
    cost = output.get("cost_usd")
    settled = isinstance(status, str) and status in SETTLED_BY_THE_RUN
    if not settled or not isinstance(item, str) or not isinstance(cost, str):
        raise ApplicationError(
            steps.RESULT_UNREADABLE, type=steps.WRITING_FAILED, non_retryable=True
        )
    try:
        item_id = uuid.UUID(item)
    except ValueError:
        raise ApplicationError(
            steps.RESULT_UNREADABLE, type=steps.WRITING_FAILED, non_retryable=True
        ) from None
    return steps.RunOutcome(run=run, content_item_id=item_id, status=str(status), cost_usd=cost)
