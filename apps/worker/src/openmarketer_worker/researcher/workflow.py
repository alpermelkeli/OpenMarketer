"""``Research``: one requested research run, from picked up to finished.

The workflow is the researcher's agent run by the agent runtime
(``agent_workflow``): it takes the steps of the agent's graph, runs the search
and the fetch the graph asks for as activities of their own, and forgets the
run's checkpoints whatever happened. What is the researcher's own is here, in
two hooks, and decides nothing about the product:

- ``before`` marks the run started and takes, from that activity, the limits
  this worker was configured with for the steps of the graph: workflow code
  cannot read the environment;
- ``failed`` records why on the run. A run that ends without a report must not
  stay unfinished, or its checkpoints would stay with it; so every way a run
  can end without a result (a submission that cannot be read, attempts used
  up, a timeout, a worker that died, a cancellation) leads here.

The workflow keeps its name, its input and its id, which the API starts it
with.

It does not pass page text, questions or credentials: the history holds the
three identifiers of the run, the limits, a query, a URL, page ids with their
flags, short messages and, for a failed activity, the stack trace Temporal
records (file names and source lines of the worker, no values). It does not
cancel a step that is already running in a worker, and it cannot record
anything when the workflow itself is terminated.
"""

from __future__ import annotations

from temporalio import workflow

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
    from openmarketer_worker.agent_runtime.workflow_support import database_write
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

    async def failed(self, run: ResearchRunInput, reason: str) -> None:
        await database_write(
            steps.RECORD_FAILURE,
            steps.RunFailure(run=run, error=steps.in_the_researchers_words(reason)),
            policy.RECORD_FAILURE,
            not_retried=steps.NOT_RETRIED,
        )
