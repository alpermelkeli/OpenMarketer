"""What the workflow of any agent needs to call a database write and to word a failure.

``database_write`` runs an activity that is one short transaction, with the
timeouts and retries of its policy. ``reason_shown`` turns whatever stopped an
activity (or the workflow itself) into a text that may be stored on a run and
shown to the person who asked for it.

This is workflow code: deterministic, with nothing that reaches the network,
the file system, the clock or the environment, and the sandbox loads it. It
holds no agent's wording and no failure type of an agent: both come in as
arguments (``FailureTexts``, the types an agent's activities word themselves).
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ApplicationError,
    TimeoutError,
    TimeoutType,
    is_cancelled_exception,
)

with workflow.unsafe.imports_passed_through():
    from openmarketer_worker.agent_runtime.vocabulary import (
        RETRY_BACKOFF,
        DatabaseWritePolicy,
        FailureTexts,
    )


async def database_write(
    step: str,
    argument: object,
    limits: DatabaseWritePolicy,
    *,
    not_retried: Collection[str],
    result_type: type | None = None,
) -> Any:
    """Run the activity ``step`` under ``limits``; failures of ``not_retried`` types end it."""
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
            backoff_coefficient=RETRY_BACKOFF,
            maximum_interval=timedelta(seconds=limits.max_retry_wait_seconds),
            maximum_attempts=limits.max_attempts,
            non_retryable_error_types=list(not_retried),
        ),
    )


def reason_shown(
    failure: BaseException,
    texts: FailureTexts,
    *,
    worded_types: Collection[str] | None = None,
) -> str:
    """The reason to show for ``failure``: the activity's own wording, or a fixed text.

    A failure raised by an activity (or by the workflow) carries a message
    written for whoever reads it when its type is one of ``worded_types``, or
    whatever its type is when that is ``None``: the activities of an agent that
    word every failure they raise. Anything else (a timeout, a worker that
    stopped, an error nobody worded) is told in the fixed words of ``texts``,
    so a message written for a log never reaches a run.
    """
    if is_cancelled_exception(failure):
        return texts.cancelled
    reason = failure if isinstance(failure, ApplicationError) else failure.__cause__
    if isinstance(reason, ApplicationError) and (
        worded_types is None or reason.type in worded_types
    ):
        # An empty message tells nothing, and a failed outcome must say what went wrong.
        return reason.message or texts.stopped_unexpectedly
    if isinstance(reason, TimeoutError):
        return texts.worker_stopped if reason.type is TimeoutType.HEARTBEAT else texts.timed_out
    return texts.stopped_unexpectedly
