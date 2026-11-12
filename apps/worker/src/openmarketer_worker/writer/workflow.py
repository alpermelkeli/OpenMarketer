"""``WritePost``: one requested writing run, from picked up to finished.

The workflow is the writer's agent run by the agent runtime (``agent_workflow``): it takes the
steps of the agent's graph, runs the two actions the graph asks for as activities of their own
(``store_draft`` at once, ``publish_post`` only once a person approved it at the policy gate),
and forgets the run's checkpoints whatever happened. What is the writer's own is here, in three
hooks, and decides nothing about the post:

- ``before`` marks the run started and takes, from that activity, the limits this worker was
  configured with for the steps of the graph: workflow code cannot read the environment;
- ``after`` settles the run's post as the graph's result says it ended (published, recorded
  as a dry run, rejected or expired) and marks the run succeeded, both in one write, then tells
  the chat that asked for the run, if one did. A post that a person approved and that could
  not be published or recorded, and a result that cannot be read, end the run as a failure
  instead, which ``failed`` records;
- ``failed`` records why on the run, with what its stored draft cost; a post the run proposed
  and nobody settled is settled ``failed`` with it. Every way a run can end without a settled
  post (no approved profile, a model's answers that cannot be read, attempts used up, a
  timeout, a worker that died, a cancellation, the post that could not be published) leads
  here. A post whose send started and was never confirmed (the platform stayed unavailable
  through every attempt) stays ``publishing``, since the platform may have it, and the run is
  told so first (``record_failure``). Once that is recorded, it tells the chat.

Telling the chat is ``record_end``, which stores the run's last event (``finished`` with the
content item and how its post ended; ``failed`` with the reason recorded on the run) and answers
with the chat that asked for the run (``chat_thread_id``, the chat's ``start_writing``); the
workflow then starts the chat's follow-up turn (``ChatFollowUp``, of kind ``WRITING_RUN_KIND``)
as a child it abandons, with an id derived from the run, so a run that is told twice follows up
once. Neither can change how the run ended: a failure of either is logged and the hook returns
as if it had worked. A run another run asked for (a task's step) names no chat, and its task
hears of its end instead.

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
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, ApplicationError, WorkflowAlreadyStartedError
from temporalio.workflow import ChildWorkflowCancellationType, ParentClosePolicy

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
    from openmarketer_worker.agent_runtime.workflow_support import (
        database_write,
        reason_shown,
        until_it_ends,
    )
    from openmarketer_worker.orchestrator.contract import (
        CHAT_FOLLOW_UP_TASK_QUEUE,
        CHAT_FOLLOW_UP_WORKFLOW,
        ChatFollowUpInput,
        follow_up_workflow_id,
    )
    from openmarketer_worker.writer import policy, steps
    from openmarketer_worker.writer.policy import WriterPolicy

# How a post that a person decided on may end a run that succeeds.
SETTLED_BY_THE_RUN = (
    PostStatus.PUBLISHED,
    PostStatus.DRY_RUN,
    PostStatus.REJECTED,
    PostStatus.EXPIRED,
)


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
        # The post is settled: a cancellation that comes now waits for the write and for the
        # chat to be told, and does not turn a run whose post a person decided on into one
        # that failed.
        await until_it_ends(settle_and_tell(outcome))

    async def failed(self, run: WritingRunInput, reason: str) -> None:
        error = steps.in_the_writers_words(reason)
        await database_write(
            steps.RECORD_FAILURE,
            steps.RunFailure(run=run, error=error),
            policy.RECORD_FAILURE,
            not_retried=steps.NOT_RETRIED,
        )
        await tell_the_chat(steps.RunEnd(run=run, reason=error))


async def settle_and_tell(outcome: steps.RunOutcome) -> None:
    """Settle the run's post and end the run, then tell the chat that asked for it."""
    await database_write(
        steps.RECORD_OUTCOME,
        outcome,
        policy.RECORD_OUTCOME,
        not_retried=steps.NOT_RETRIED,
    )
    await tell_the_chat(
        steps.RunEnd(
            run=outcome.run, content_item_id=outcome.content_item_id, post_status=outcome.status
        )
    )


async def tell_the_chat(end: steps.RunEnd) -> None:
    """Record how the run ended and start the follow-up of its chat; never raises for either."""
    try:
        told: steps.RunEndRecorded = await database_write(
            steps.RECORD_END,
            end,
            policy.RECORD_END,
            not_retried=steps.NOT_RETRIED,
            result_type=steps.RunEndRecorded,
        )
    except ActivityError as failure:
        # The run ended as it did; a chat that is not told is not worth changing that.
        workflow.logger.warning(
            "the end of writing run %s was not recorded: %s",
            end.run.run_id,
            reason_shown(failure, steps.WRITING_TEXTS, worded_types=steps.WORDED_BY_THE_ACTIVITY),
        )
        return
    if told.chat_id is not None:
        await start_follow_up(end.run, told.chat_id)


async def start_follow_up(run: WritingRunInput, chat_id: uuid.UUID) -> None:
    """Start the chat's follow-up turn on ``run``, once: a second start finds the first."""
    try:
        await workflow.start_child_workflow(
            CHAT_FOLLOW_UP_WORKFLOW,
            ChatFollowUpInput(
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                chat_id=chat_id,
                run_kind=WRITING_RUN_KIND,
                run_id=run.run_id,
            ),
            id=follow_up_workflow_id(WRITING_RUN_KIND, run.run_id),
            task_queue=CHAT_FOLLOW_UP_TASK_QUEUE,
            # The follow-up outlives the run, and is not cancelled with it.
            cancellation_type=ChildWorkflowCancellationType.ABANDON,
            parent_close_policy=ParentClosePolicy.ABANDON,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        workflow.logger.info("writing run %s was followed up on before", run.run_id)


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
