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
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    TimeoutError,
    TimeoutType,
    is_cancelled_exception,
)

with workflow.unsafe.imports_passed_through():
    from openmarketer_core.analysis_workflow import (
        ANALYZE_REPOSITORY_WORKFLOW,
        AnalyzeRepositoryInput,
    )
    from openmarketer_worker import policy, steps
    from openmarketer_worker.policy import AnalysisPolicy, DatabaseWritePolicy


@workflow.defn(name=ANALYZE_REPOSITORY_WORKFLOW)
class AnalyzeRepository:
    @workflow.run
    async def run(self, run: AnalyzeRepositoryInput) -> None:
        try:
            analysis_policy: AnalysisPolicy = await _database_write(
                steps.START_RUN, run, policy.START_RUN, result_type=AnalysisPolicy
            )
            await workflow.execute_activity(
                steps.ANALYSE_AND_STORE,
                run,
                start_to_close_timeout=timedelta(seconds=analysis_policy.attempt_timeout_seconds),
                heartbeat_timeout=timedelta(seconds=analysis_policy.heartbeat_timeout_seconds),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=analysis_policy.retry_after_seconds),
                    backoff_coefficient=2.0,
                    maximum_attempts=analysis_policy.max_attempts,
                    non_retryable_error_types=list(steps.NOT_RETRIED),
                ),
            )
        except (ActivityError, asyncio.CancelledError) as failure:
            failed = steps.RunFailure(run=run, error=_shown(failure))
            # Shielded: when the workflow was cancelled, the record must still be written.
            await asyncio.shield(
                _database_write(steps.RECORD_FAILURE, failed, policy.RECORD_FAILURE)
            )
            raise


async def _database_write(
    step: str, argument: object, limits: DatabaseWritePolicy, *, result_type: type | None = None
) -> Any:
    give_up_after = limits.give_up_after_seconds
    return await workflow.execute_activity(
        step,
        argument,
        result_type=result_type,
        start_to_close_timeout=timedelta(seconds=limits.attempt_timeout_seconds),
        schedule_to_close_timeout=(
            None if give_up_after is None else timedelta(seconds=give_up_after)
        ),
        retry_policy=RetryPolicy(
            initial_interval=timedelta(seconds=limits.retry_after_seconds),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(seconds=limits.max_retry_wait_seconds),
            maximum_attempts=limits.max_attempts,
            non_retryable_error_types=list(steps.NOT_RETRIED),
        ),
    )


def _shown(failure: BaseException) -> str:
    """The reason recorded on the run: the activity's own wording, or a fixed text."""
    if is_cancelled_exception(failure):
        return steps.CANCELLED
    cause = failure.__cause__
    if isinstance(cause, ApplicationError) and cause.type in steps.WORDED_BY_THE_ACTIVITY:
        return cause.message
    if isinstance(cause, TimeoutError):
        return steps.WORKER_STOPPED if cause.type is TimeoutType.HEARTBEAT else steps.TIMED_OUT
    return steps.STOPPED_UNEXPECTEDLY
