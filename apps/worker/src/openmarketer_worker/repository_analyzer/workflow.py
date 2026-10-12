"""``AnalyzeRepository``: one requested analysis run, from picked up to finished.

The workflow is the analyzer's agent run by the agent runtime (``agent_workflow``): it takes
the steps of the agent's graph, runs the clone the graph asks for as an activity of its own,
and forgets the run's checkpoints whatever happened. What is the analyzer's own is here, in
two hooks, and decides nothing about the product:

- ``before`` marks the run started and takes, from that activity, the limits this worker was
  configured with for the steps of the graph: workflow code cannot read the environment;
- ``failed`` records why on the run. A run must never stay unfinished, because an unfinished
  run blocks its project; so every way a run can end without a result (a refused repository,
  attempts used up, a timeout, a worker that died, a cancellation) leads here.

The workflow keeps its name, its input and its id, which the API starts it with.

It does not pass repository content, profiles or credentials: the history holds the three
identifiers of the run, the limits, the commit that was cloned, the id of the stored profile,
short messages and, for a failed activity, the stack trace Temporal records (file names and
source lines of the worker, no values). It does not cancel a step that is already running in
a worker, and it cannot record anything when the workflow itself is terminated.
"""

from __future__ import annotations

from temporalio import workflow

from openmarketer_worker.agent_runtime.workflow_dsl import agent_workflow

with workflow.unsafe.imports_passed_through():
    from openmarketer_core.agent_runtime.steps import GraphRunRef
    from openmarketer_core.repository_analysis.analyzer_agent.definitions import agent
    from openmarketer_core.repository_analysis.workflow_contract import (
        ANALYSIS_RUN_KIND,
        ANALYZE_REPOSITORY_WORKFLOW,
        AnalyzeRepositoryInput,
    )
    from openmarketer_worker.agent_runtime.vocabulary import GraphRunPolicy
    from openmarketer_worker.agent_runtime.workflow_support import database_write
    from openmarketer_worker.repository_analyzer import policy, steps
    from openmarketer_worker.repository_analyzer.policy import AnalysisPolicy


def run_of(run: AnalyzeRepositoryInput) -> GraphRunRef:
    """The run of the analyzer's graph that the workflow's input names."""
    return GraphRunRef(kind=ANALYSIS_RUN_KIND, run_id=run.run_id)


def graph_limits(analysis: AnalysisPolicy) -> GraphRunPolicy:
    return GraphRunPolicy(
        attempt_timeout_seconds=analysis.attempt_timeout_seconds,
        heartbeat_timeout_seconds=analysis.heartbeat_timeout_seconds,
        max_attempts=analysis.max_attempts,
        retry_after_seconds=analysis.retry_after_seconds,
    )


@agent_workflow(
    agent,
    name=ANALYZE_REPOSITORY_WORKFLOW,
    input=AnalyzeRepositoryInput,
    run_of=run_of,
    # What a run is taken with when ``before`` gives nothing else, which it always does.
    graph=graph_limits(AnalysisPolicy()),
    texts=steps.ANALYSIS_TEXTS,
    worded_types=steps.WORDED_BY_THE_ACTIVITY,
)
class AnalyzeRepository:
    async def before(self, run: AnalyzeRepositoryInput) -> GraphRunPolicy:
        configured: AnalysisPolicy = await database_write(
            steps.START_RUN,
            run,
            policy.START_RUN,
            not_retried=steps.NOT_RETRIED,
            result_type=AnalysisPolicy,
        )
        return graph_limits(configured)

    async def failed(self, run: AnalyzeRepositoryInput, reason: str) -> None:
        await database_write(
            steps.RECORD_FAILURE,
            steps.RunFailure(run=run, error=steps.in_the_analyzers_words(reason)),
            policy.RECORD_FAILURE,
            not_retried=steps.NOT_RETRIED,
        )
