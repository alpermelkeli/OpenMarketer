"""``OrchestratorTask``: one requested task, from picked up to its report.

The workflow is the task's agent run by the agent runtime (``agent_workflow``): it takes the
steps of the agent's graph, holds ``confirm_plan`` at the policy gate until a person confirms
or rejects the plan, starts each step's run as a child workflow and waits for it with no graph
running (the runtime's run actions: each run's record is made first, keyed by the action, and
its step is recorded as dispatched with it), and forgets the task's checkpoints whatever
happened. What is the task's own is here, in three hooks, and decides nothing about the plan:

- ``before`` marks the task started and takes, from that activity, the limits this worker was
  configured with for the steps of the graph (workflow code cannot read the environment) and
  the chat that asked for the task;
- ``went_on`` hears of each step whose run ended while the task goes on (the graph asked for
  another action after it), and a task with a chat starts that chat's follow-up turn on the
  step (``ChatFollowUp`` with the step's id), so the thread is told the task's progress as it
  goes. The step that ends the task has none of its own: the report's follow-up sums it up;
- ``after`` records the task's last event once its graph finished. The plan, its changes, how
  each step's run ended and the report were stored by then, with the task marked succeeded,
  inside the step that finished the graph (``task.py``'s ``finish``), which holds the final
  state: the history then carries an outcome and an amount, never a plan or a report;
- ``failed`` records why on the task, with what its own model calls cost. Every way a task can
  end without a report (a plan that cannot be read, attempts used up, a timeout, a worker that
  died, a cancellation) leads here. A run a step started keeps going on its own, or is
  cancelled with the task when the task waited for it.

Either way, a task that a chat asked for then starts that chat's follow-up turn
(``ChatFollowUp``, of kind ``TASK_RUN_KIND``) as a child it abandons, with an id derived from
the task, so a task that is told twice follows up once. Neither can change how the task ended:
a failure of either is logged and the hook returns as if it had worked.

The follow-ups of a step are children it abandons too, each with an id derived from the task
and the step, so a retried workflow does not start a second one for the same step. A chat takes
one turn at a time, and a follow-up that meets a busy chat is tried again a little later, so two
follow-ups running together could be taken in either order: ``ChatFollowUps`` starts each one
only once the one before it has ended, in a task of the workflow's own so that the task goes on
meanwhile, and the report's only once the last step's has ended. The task's workflow so lasts
until its chat has been told everything, in order. Starting these children is new in the
history of a task: a task that was running when this changed is not replayed by this code
(there is no versioning).

``scope_of`` names the task's workspace and project: the proposal of its plan and the runs it
starts belong there. The history holds the task's identifiers, the limits, the child runs'
identifiers and inputs (identifiers only), their small results, statuses, an amount, short
messages and, for a failed activity, the stack trace Temporal records. The actions' payloads
are the exception the agent's declarations bound: the plan ``confirm_plan`` asks a person to
confirm, and the request of each step's run, which the history of an action carries as its
request. No operator's request, profile or credential is in it.

The definitions are imported before any module of the worker: the agent's declarations must be
the sandbox's own copy, loaded first, so the agent the runtime's modules see is this one.
"""

import asyncio
import uuid
from typing import Any

from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    ChildWorkflowError,
    WorkflowAlreadyStartedError,
)
from temporalio.workflow import (
    ChildWorkflowCancellationType,
    ChildWorkflowHandle,
    ParentClosePolicy,
)

# One block, in which every module of core (the agent's declarations among them) comes before
# every module of the worker.
with workflow.unsafe.imports_passed_through():
    from openmarketer_core.agent_runtime.actions import ActionRequest
    from openmarketer_core.agent_runtime.gate import RunScope
    from openmarketer_core.agent_runtime.steps import GraphRunRef
    from openmarketer_core.orchestrator.task.task_agent.definitions import agent
    from openmarketer_core.orchestrator.task.workflow_contract import (
        TASK_RUN_KIND,
        TASK_WORKFLOW,
        TaskRunInput,
    )
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
    from openmarketer_worker.orchestrator_task import policy, steps
    from openmarketer_worker.orchestrator_task.policy import TaskPolicy


def run_of(run: TaskRunInput) -> GraphRunRef:
    """The run of the task's graph that the workflow's input names."""
    return GraphRunRef(kind=TASK_RUN_KIND, run_id=run.run_id)


def scope_of(run: TaskRunInput) -> RunScope:
    """The workspace and project whose proposals and runs the task's actions belong to."""
    return RunScope(workspace_id=run.workspace_id, project_id=run.project_id)


def graph_limits(task: TaskPolicy) -> GraphRunPolicy:
    return GraphRunPolicy(
        attempt_timeout_seconds=task.attempt_timeout_seconds,
        heartbeat_timeout_seconds=task.heartbeat_timeout_seconds,
        max_attempts=task.max_attempts,
        retry_after_seconds=task.retry_after_seconds,
    )


@agent_workflow(
    agent,
    name=TASK_WORKFLOW,
    input=TaskRunInput,
    run_of=run_of,
    scope_of=scope_of,
    # What a task is taken with when ``before`` gives nothing else, which it always does.
    graph=graph_limits(TaskPolicy()),
    texts=steps.TASK_TEXTS,
    worded_types=steps.WORDED_BY_THE_ACTIVITY,
)
class OrchestratorTask:
    def __init__(self) -> None:
        # The chat that asked for the task, once ``before`` has read it, and its follow-ups.
        self._chat_id: uuid.UUID | None = None
        self._follow_ups = ChatFollowUps()

    async def before(self, run: TaskRunInput) -> GraphRunPolicy:
        started: steps.TaskStarted = await database_write(
            steps.START_TASK,
            run,
            policy.START_TASK,
            not_retried=steps.NOT_RETRIED,
            result_type=steps.TaskStarted,
        )
        self._chat_id = started.chat_id
        return graph_limits(started.policy)

    async def went_on(self, run: TaskRunInput, action: ActionRequest) -> None:
        step = action.payload.get("step")
        if self._chat_id is not None and isinstance(step, str):
            self._follow_ups.tell(run, self._chat_id, step)

    async def after(self, run: TaskRunInput, output: dict[str, Any]) -> None:
        outcome = output.get("outcome")
        if not isinstance(outcome, str) or not outcome:
            raise ApplicationError(
                steps.RESULT_UNREADABLE, type=steps.TASK_FAILED, non_retryable=True
            )
        # The task has its report: a cancellation that comes now waits for the chat to be
        # told, and does not turn a task that ended into one that failed.
        await until_it_ends(
            tell_the_chat(steps.TaskEnd(run=run, outcome=outcome), self._follow_ups)
        )

    async def failed(self, run: TaskRunInput, reason: str) -> None:
        error = steps.in_the_tasks_words(reason)
        told: steps.TaskEndRecorded = await database_write(
            steps.RECORD_FAILURE,
            steps.TaskEnd(run=run, reason=error),
            policy.RECORD_FAILURE,
            not_retried=steps.NOT_RETRIED,
            result_type=steps.TaskEndRecorded,
        )
        if told.chat_id is not None:
            await self._follow_ups.told()
            await start_follow_up(run, told.chat_id)


class ChatFollowUps:
    """The follow-ups of the steps of one task, each started once the one before has ended.

    Each runs in a task of the workflow's own, so the task's graph goes on meanwhile; ``told``
    waits for the last of them, which the report's follow-up is started after.
    """

    def __init__(self) -> None:
        self._last: asyncio.Task[None] | None = None

    def tell(self, run: TaskRunInput, chat_id: uuid.UUID, step: str) -> None:
        """Start the chat's follow-up on ``step`` once every one before it has ended."""
        self._last = asyncio.create_task(_after(self._last, run, chat_id, step))

    async def told(self) -> None:
        """Wait until every step's follow-up started so far has ended."""
        if self._last is not None:
            await self._last


async def _after(
    earlier: asyncio.Task[None] | None, run: TaskRunInput, chat_id: uuid.UUID, step: str
) -> None:
    if earlier is not None:
        await earlier
    started = await start_follow_up(run, chat_id, step)
    if started is None:
        return
    try:
        await started
    except ChildWorkflowError:
        # The follow-up tells the chat itself when it cannot; nothing waits for more.
        workflow.logger.info("the follow-up of step %s of task %s failed", step, run.run_id)


async def tell_the_chat(end: steps.TaskEnd, follow_ups: ChatFollowUps) -> None:
    """Record the task's last event and start the follow-up of its chat; never raises."""
    try:
        told: steps.TaskEndRecorded = await database_write(
            steps.RECORD_END,
            end,
            policy.RECORD_END,
            not_retried=steps.NOT_RETRIED,
            result_type=steps.TaskEndRecorded,
        )
    except ActivityError as failure:
        # The task ended as it did; a chat that is not told is not worth changing that.
        workflow.logger.warning(
            "the end of task %s was not recorded: %s",
            end.run.run_id,
            reason_shown(failure, steps.TASK_TEXTS, worded_types=steps.WORDED_BY_THE_ACTIVITY),
        )
        return
    if told.chat_id is not None:
        await follow_ups.told()
        await start_follow_up(end.run, told.chat_id)


async def start_follow_up(
    run: TaskRunInput, chat_id: uuid.UUID, step: str | None = None
) -> ChildWorkflowHandle[Any, Any] | None:
    """Start the chat's follow-up turn on ``run``, or on its step ``step``, once.

    The follow-up started, or ``None`` when one was started before: a second start finds the
    first, which is not waited for then.
    """
    try:
        return await workflow.start_child_workflow(
            CHAT_FOLLOW_UP_WORKFLOW,
            ChatFollowUpInput(
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                chat_id=chat_id,
                run_kind=TASK_RUN_KIND,
                run_id=run.run_id,
                step=step,
            ),
            id=follow_up_workflow_id(TASK_RUN_KIND, run.run_id, step),
            task_queue=CHAT_FOLLOW_UP_TASK_QUEUE,
            # The follow-up outlives the task, and is not cancelled with it.
            cancellation_type=ChildWorkflowCancellationType.ABANDON,
            parent_close_policy=ParentClosePolicy.ABANDON,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        followed = f"task {run.run_id}" if step is None else f"step {step} of task {run.run_id}"
        workflow.logger.info("%s was followed up on before", followed)
        return None
