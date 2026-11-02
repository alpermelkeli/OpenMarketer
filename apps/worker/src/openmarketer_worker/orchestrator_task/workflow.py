"""``OrchestratorTask``: one requested task, from picked up to its report.

The workflow is the task's agent run by the agent runtime (``agent_workflow``): it takes the
steps of the agent's graph, holds ``confirm_plan`` at the policy gate until a person confirms
or rejects the plan, starts each step's run as a child workflow and waits for it with no graph
running (the runtime's run actions: each run's record is made first, keyed by the action, and
its step is recorded as dispatched with it), and forgets the task's checkpoints whatever
happened. What is the task's own is here, in three hooks, and decides nothing about the plan:

- ``before`` marks the task started and takes, from that activity, the limits this worker was
  configured with for the steps of the graph: workflow code cannot read the environment;
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
    async def before(self, run: TaskRunInput) -> GraphRunPolicy:
        configured: TaskPolicy = await database_write(
            steps.START_TASK,
            run,
            policy.START_TASK,
            not_retried=steps.NOT_RETRIED,
            result_type=TaskPolicy,
        )
        return graph_limits(configured)

    async def after(self, run: TaskRunInput, output: dict[str, Any]) -> None:
        outcome = output.get("outcome")
        if not isinstance(outcome, str) or not outcome:
            raise ApplicationError(
                steps.RESULT_UNREADABLE, type=steps.TASK_FAILED, non_retryable=True
            )
        # The task has its report: a cancellation that comes now waits for the chat to be
        # told, and does not turn a task that ended into one that failed.
        await until_it_ends(tell_the_chat(steps.TaskEnd(run=run, outcome=outcome)))

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
            await start_follow_up(run, told.chat_id)


async def tell_the_chat(end: steps.TaskEnd) -> None:
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
        await start_follow_up(end.run, told.chat_id)


async def start_follow_up(run: TaskRunInput, chat_id: uuid.UUID) -> None:
    """Start the chat's follow-up turn on ``run``, once: a second start finds the first."""
    try:
        await workflow.start_child_workflow(
            CHAT_FOLLOW_UP_WORKFLOW,
            ChatFollowUpInput(
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                chat_id=chat_id,
                run_kind=TASK_RUN_KIND,
                run_id=run.run_id,
            ),
            id=follow_up_workflow_id(TASK_RUN_KIND, run.run_id),
            task_queue=CHAT_FOLLOW_UP_TASK_QUEUE,
            # The follow-up outlives the task, and is not cancelled with it.
            cancellation_type=ChildWorkflowCancellationType.ABANDON,
            parent_close_policy=ParentClosePolicy.ABANDON,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        workflow.logger.info("task %s was followed up on before", run.run_id)
