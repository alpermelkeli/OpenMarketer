"""``CreativeStudio``: one requested studio run, from picked up to finished.

The workflow is the studio's agent run by the agent runtime (``agent_workflow``): it takes
the steps of the agent's graph, runs the operations the graph asks for as activities of their
own (a paid generation or edit only once the creative budget, or a person, let it through),
and forgets the run's checkpoints whatever happened. What is the studio's own is here, in
three hooks, and decides nothing about the product:

- ``before`` marks the run started and takes, from that activity, the limits this worker was
  configured with for the steps of the graph: workflow code cannot read the environment;
- ``after`` tells the chat that asked for the run, if one did, that it ended with an asset;
- ``failed`` records why on the run, with what its stored images cost. Every way a run can
  end without an asset (a brief that cannot be read, attempts used up, a timeout, a worker
  that died, a cancellation) leads here. Once that is recorded, it tells the chat.

Telling the chat is ``record_end``, which stores the run's last event (``finished`` with the
asset, the version and whether it waits for a person; ``failed`` with the reason recorded on
the run) and answers with the chat; the workflow then starts the chat's follow-up turn
(``ChatFollowUp``, of kind ``STUDIO_RUN_KIND``) as a child it abandons, with an id derived
from the run, so a run that is told twice follows up once. Neither can change how the run
ended: a failure of either is logged and the hook returns as if it had worked.

``scope_of`` names the run's workspace and project: the proposals and the spends of its paid
operations belong there. The history holds the run's identifiers, the limits, version and
asset ids, sizes, amounts, short messages and, for a failed activity, the stack trace Temporal
records; never a prompt, a page, a program, a read text or an image (the payloads of the
operations are the exception the agent's declarations bound: a prompt, a page or a program the
studio's model wrote, which the history of an action carries as its request).

The definitions are imported before any module of the worker: the agent's declarations must
be the sandbox's own copy, loaded first, so the agent the runtime's modules see is this one.
"""

from __future__ import annotations

import uuid
from typing import Any

from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, WorkflowAlreadyStartedError
from temporalio.workflow import ChildWorkflowCancellationType, ParentClosePolicy

# One block, in which every module of core (the agent's declarations among them) comes before
# every module of the worker.
with workflow.unsafe.imports_passed_through():
    from openmarketer_core.agent_runtime.gate import RunScope
    from openmarketer_core.agent_runtime.steps import GraphRunRef
    from openmarketer_core.studio.studio_agent.definitions import agent
    from openmarketer_core.studio.workflow_contract import (
        STUDIO_RUN_KIND,
        STUDIO_WORKFLOW,
        StudioRunInput,
    )
    from openmarketer_worker.agent_runtime.vocabulary import GraphRunPolicy
    from openmarketer_worker.agent_runtime.workflow_dsl import agent_workflow
    from openmarketer_worker.agent_runtime.workflow_support import (
        database_write,
        reason_shown,
        until_it_ends,
    )
    from openmarketer_worker.creative_studio import policy, steps
    from openmarketer_worker.creative_studio.policy import StudioPolicy
    from openmarketer_worker.orchestrator.contract import (
        CHAT_FOLLOW_UP_TASK_QUEUE,
        CHAT_FOLLOW_UP_WORKFLOW,
        ChatFollowUpInput,
        follow_up_workflow_id,
    )


def run_of(run: StudioRunInput) -> GraphRunRef:
    """The run of the studio's graph that the workflow's input names."""
    return GraphRunRef(kind=STUDIO_RUN_KIND, run_id=run.run_id)


def scope_of(run: StudioRunInput) -> RunScope:
    """The workspace and project whose budget and proposals the run's paid operations use."""
    return RunScope(workspace_id=run.workspace_id, project_id=run.project_id)


def graph_limits(studio: StudioPolicy) -> GraphRunPolicy:
    return GraphRunPolicy(
        attempt_timeout_seconds=studio.attempt_timeout_seconds,
        heartbeat_timeout_seconds=studio.heartbeat_timeout_seconds,
        max_attempts=studio.max_attempts,
        retry_after_seconds=studio.retry_after_seconds,
    )


@agent_workflow(
    agent,
    name=STUDIO_WORKFLOW,
    input=StudioRunInput,
    run_of=run_of,
    scope_of=scope_of,
    # What a run is taken with when ``before`` gives nothing else, which it always does.
    graph=graph_limits(StudioPolicy()),
    texts=steps.STUDIO_TEXTS,
    worded_types=steps.WORDED_BY_THE_ACTIVITY,
)
class CreativeStudio:
    async def before(self, run: StudioRunInput) -> GraphRunPolicy:
        configured: StudioPolicy = await database_write(
            steps.START_RUN,
            run,
            policy.START_RUN,
            not_retried=steps.NOT_RETRIED,
            result_type=StudioPolicy,
        )
        return graph_limits(configured)

    async def after(self, run: StudioRunInput, output: dict[str, Any]) -> None:
        end = steps.RunEnd(
            run=run,
            asset_id=_text_or_none(output.get("asset_id")),
            version_id=_text_or_none(output.get("version_id")),
            needs_review=output.get("needs_review") is True,
        )
        # The run has its asset: a cancellation that comes now waits for the chat to be told,
        # and does not turn a run that ended into one that failed.
        await until_it_ends(tell_the_chat(end))

    async def failed(self, run: StudioRunInput, reason: str) -> None:
        error = steps.in_the_studios_words(reason)
        await database_write(
            steps.RECORD_FAILURE,
            steps.RunFailure(run=run, error=error),
            policy.RECORD_FAILURE,
            not_retried=steps.NOT_RETRIED,
        )
        await tell_the_chat(steps.RunEnd(run=run, reason=error))


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


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
            "the end of studio run %s was not recorded: %s",
            end.run.run_id,
            reason_shown(failure, steps.STUDIO_TEXTS, worded_types=steps.WORDED_BY_THE_ACTIVITY),
        )
        return
    if told.chat_id is not None:
        await start_follow_up(end.run, told.chat_id)


async def start_follow_up(run: StudioRunInput, chat_id: uuid.UUID) -> None:
    """Start the chat's follow-up turn on ``run``, once: a second start finds the first."""
    try:
        await workflow.start_child_workflow(
            CHAT_FOLLOW_UP_WORKFLOW,
            ChatFollowUpInput(
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                chat_id=chat_id,
                run_kind=STUDIO_RUN_KIND,
                run_id=run.run_id,
            ),
            id=follow_up_workflow_id(STUDIO_RUN_KIND, run.run_id),
            task_queue=CHAT_FOLLOW_UP_TASK_QUEUE,
            # The follow-up outlives the run, and is not cancelled with it.
            cancellation_type=ChildWorkflowCancellationType.ABANDON,
            parent_close_policy=ParentClosePolicy.ABANDON,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        workflow.logger.info("studio run %s was followed up on before", run.run_id)
