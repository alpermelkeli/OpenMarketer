"""``AnalyzeRepository``: one requested analysis run, from picked up to finished.

The workflow only orders three activities and decides nothing about the
product: mark the run started, analyse the repository and store the result,
and, if either did not work, record why on the run. A run must never stay
unfinished, because an unfinished run blocks its project; so every way the
first two steps can end without a result (a refused repository, attempts used
up, a timeout, a worker that died, a cancellation) leads to the third.

It does not pass repository content, profiles or credentials: the history
holds the three identifiers of the run, the limits, short messages and, for a
failed activity, the stack trace Temporal records (file names and source lines
of the worker, no values). It does not cancel
an analysis that is already running in a worker, and it cannot record anything
when the workflow itself is terminated.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from openmarketer_core.repository_analysis.workflow_contract import (
        ANALYZE_REPOSITORY_WORKFLOW,
        AnalyzeRepositoryInput,
    )
    from openmarketer_worker.agent_runtime.workflow_support import database_write, reason_shown
    from openmarketer_worker.repository_analyzer import policy, steps
    from openmarketer_worker.repository_analyzer.policy import AnalysisPolicy


@workflow.defn(name=ANALYZE_REPOSITORY_WORKFLOW)
class AnalyzeRepository:
    @workflow.run
    async def run(self, run: AnalyzeRepositoryInput) -> None:
        try:
            analysis_policy: AnalysisPolicy = await database_write(
                steps.START_RUN,
                run,
                policy.START_RUN,
                not_retried=steps.NOT_RETRIED,
                result_type=AnalysisPolicy,
            )
            await workflow.execute_activity(
                steps.ANALYSE_AND_STORE,
                run,
                start_to_close_timeout=timedelta(seconds=analysis_policy.attempt_timeout_seconds),
                heartbeat_timeout=timedelta(seconds=analysis_policy.heartbeat_timeout_seconds),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=analysis_policy.retry_after_seconds),
                    backoff_coefficient=policy.ANALYSIS_RETRY_BACKOFF,
                    maximum_attempts=analysis_policy.max_attempts,
                    non_retryable_error_types=list(steps.NOT_RETRIED),
                ),
            )
        except (ActivityError, asyncio.CancelledError) as failure:
            reason = reason_shown(
                failure, steps.ANALYSIS_TEXTS, worded_types=steps.WORDED_BY_THE_ACTIVITY
            )
            failed = steps.RunFailure(run=run, error=reason)
            # Shielded: when the workflow was cancelled, the record must still be written.
            await asyncio.shield(
                database_write(
                    steps.RECORD_FAILURE,
                    failed,
                    policy.RECORD_FAILURE,
                    not_retried=steps.NOT_RETRIED,
                )
            )
            raise
