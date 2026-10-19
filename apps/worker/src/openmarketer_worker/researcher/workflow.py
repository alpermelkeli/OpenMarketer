"""``Research``: one requested research run, from picked up to finished.

The workflow is the researcher's agent run by the agent runtime
(``agent_workflow``): it takes the steps of the agent's graph, runs the search
and the fetch the graph asks for as activities of their own, and forgets the
run's checkpoints whatever happened. What is the researcher's own is here, in
three hooks, and decides nothing about the product:

- ``before`` marks the run started and takes, from that activity, the limits
  this worker was configured with for the steps of the graph: workflow code
  cannot read the environment;
- ``after`` tells the chat that asked for the run, if one did, that it finished;
- ``failed`` records why on the run. A run that ends without a report must not
  stay unfinished, or its checkpoints would stay with it; so every way a run
  can end without a result (a submission that cannot be read, attempts used
  up, a timeout, a worker that died, a cancellation) leads here. Once that is
  recorded, it tells the chat that asked for the run, if one did, that it failed.

Telling the chat is ``record_end``, which stores the run's last event
(``finished`` with how many findings, ``failed`` with the reason recorded on the
run) and answers with the chat; the workflow then starts the chat's follow-up
turn (``ChatFollowUp``) as a child it abandons, with an id derived from the run's
kind and id, so a run that is told twice follows up once. Neither can change how the run
ended: a failure of either is logged and the hook returns as if it had worked.
The follow-up is started and not waited for, on the orchestrator's queue.

The workflow keeps its name, its input and its id, which the API starts it
with. Adding ``after`` and ``record_end`` changed which activities it schedules:
a run that was in progress when the worker was updated cannot be replayed by the
new code (there is no versioning).

It does not pass page text, questions or credentials: the history holds the
three identifiers of the run, the limits, a query, a URL, page ids with their
flags, a number of findings, the chat's id, short messages and, for a failed
activity, the stack trace Temporal records (file names and source lines of the
worker, no values). It does not cancel a step that is already running in a
worker, and it cannot record anything when the workflow itself is terminated.
"""

from __future__ import annotations

import uuid
from typing import Any

from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, WorkflowAlreadyStartedError
from temporalio.workflow import ChildWorkflowCancellationType, ParentClosePolicy

from openmarketer_worker.agent_runtime.workflow_dsl import agent_workflow

with workflow.unsafe.imports_passed_through():
    from openmarketer_core.agent_runtime.steps import GraphRunRef
    from openmarketer_core.research.research_agent.definitions import agent
    from openmarketer_core.research.workflow_contract import (
        RESEARCH_RUN_KIND,
        RESEARCH_WORKFLOW,
        ResearchRunInput,
    )
    from openmarketer_worker.agent_runtime.vocabulary import GraphRunPolicy
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
    from openmarketer_worker.researcher import policy, steps
    from openmarketer_worker.researcher.policy import ResearchPolicy


def run_of(run: ResearchRunInput) -> GraphRunRef:
    """The run of the researcher's graph that the workflow's input names."""
    return GraphRunRef(kind=RESEARCH_RUN_KIND, run_id=run.run_id)


def graph_limits(research: ResearchPolicy) -> GraphRunPolicy:
    return GraphRunPolicy(
        attempt_timeout_seconds=research.attempt_timeout_seconds,
        heartbeat_timeout_seconds=research.heartbeat_timeout_seconds,
        max_attempts=research.max_attempts,
        retry_after_seconds=research.retry_after_seconds,
    )


@agent_workflow(
    agent,
    name=RESEARCH_WORKFLOW,
    input=ResearchRunInput,
    run_of=run_of,
    # What a run is taken with when ``before`` gives nothing else, which it always does.
    graph=graph_limits(ResearchPolicy()),
    texts=steps.RESEARCH_TEXTS,
    worded_types=steps.WORDED_BY_THE_ACTIVITY,
)
class Research:
    async def before(self, run: ResearchRunInput) -> GraphRunPolicy:
        configured: ResearchPolicy = await database_write(
            steps.START_RUN,
            run,
            policy.START_RUN,
            not_retried=steps.NOT_RETRIED,
            result_type=ResearchPolicy,
        )
        return graph_limits(configured)

    async def after(self, run: ResearchRunInput, output: dict[str, Any]) -> None:
        findings = output.get("findings")
        end = steps.RunEnd(run=run, findings=findings if isinstance(findings, int) else None)
        # The run has its report: a cancellation that comes now waits for the chat to be told,
        # and does not turn a run that finished into one that failed.
        await until_it_ends(tell_the_chat(end))

    async def failed(self, run: ResearchRunInput, reason: str) -> None:
        error = steps.in_the_researchers_words(reason)
        await database_write(
            steps.RECORD_FAILURE,
            steps.RunFailure(run=run, error=error),
            policy.RECORD_FAILURE,
            not_retried=steps.NOT_RETRIED,
        )
        await tell_the_chat(steps.RunEnd(run=run, reason=error))


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
            "the end of research run %s was not recorded: %s",
            end.run.run_id,
            reason_shown(failure, steps.RESEARCH_TEXTS, worded_types=steps.WORDED_BY_THE_ACTIVITY),
        )
        return
    if told.chat_id is not None:
        await start_follow_up(end.run, told.chat_id)


async def start_follow_up(run: ResearchRunInput, chat_id: uuid.UUID) -> None:
    """Start the chat's follow-up turn on ``run``, once: a second start finds the first."""
    try:
        await workflow.start_child_workflow(
            CHAT_FOLLOW_UP_WORKFLOW,
            ChatFollowUpInput(
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                chat_id=chat_id,
                run_kind=RESEARCH_RUN_KIND,
                run_id=run.run_id,
            ),
            id=follow_up_workflow_id(RESEARCH_RUN_KIND, run.run_id),
            task_queue=CHAT_FOLLOW_UP_TASK_QUEUE,
            # The follow-up outlives the run, and is not cancelled with it.
            cancellation_type=ChildWorkflowCancellationType.ABANDON,
            parent_close_policy=ParentClosePolicy.ABANDON,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        workflow.logger.info("research run %s was followed up on before", run.run_id)
