"""``ChatFollowUp``: the orchestrator's own turn in a chat once a research run it started ended.

The ``Research`` workflow starts it as a child it abandons, with an id derived from the run
(``contract.follow_up_workflow_id``), so a run follows up once. It is one activity
(``follow_up_on_research``), which takes the turn: the turn's rows, its events and its
answer are core's to store. The workflow adds what Temporal owns: the activity's time
limits, its heartbeats and which failures are tried again (``policy.py``, ``steps.py``).

A follow-up that does not answer in the end leaves its turn recorded failed in the chat
(core does that at each attempt). One that never got to a turn (the chat stayed busy, the
workspace's model could not be set up, no worker took it up in time) would leave nothing
there, so the workflow then has the chat told why, in a fixed sentence chosen by the type of
the failure (``steps.NOT_TAKEN_BECAUSE``), by a second short activity; core records nothing
when a turn was started. A follow-up with nothing to follow up on tells nobody. The workflow
then completes: it logs why, in words that may be shown, and does not fail. A failed
workflow would tell nobody more than the chat already shows, and the research run it follows
up on ended long before.

This is workflow code: deterministic, and it imports no database, model or activity code.
Its history holds the four identifiers of its input and short sentences. The annotation of
``run`` is a real type, not text, because Temporal decodes the input from it: hence no
``from __future__ import annotations``.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    TimeoutError,
    is_cancelled_exception,
)

with workflow.unsafe.imports_passed_through():
    from openmarketer_worker.agent_runtime.vocabulary import RETRY_BACKOFF
    from openmarketer_worker.agent_runtime.workflow_support import reason_shown
    from openmarketer_worker.orchestrator import policy, steps
    from openmarketer_worker.orchestrator.contract import (
        CHAT_FOLLOW_UP_WORKFLOW,
        ChatFollowUpInput,
        FollowUpNotTaken,
    )


@workflow.defn(name=CHAT_FOLLOW_UP_WORKFLOW)
class ChatFollowUp:
    @workflow.run
    async def run(self, follow_up: ChatFollowUpInput) -> None:
        limits = policy.FOLLOW_UP
        try:
            await workflow.execute_activity(
                steps.FOLLOW_UP_ON_RESEARCH,
                follow_up,
                start_to_close_timeout=timedelta(seconds=limits.attempt_timeout_seconds),
                heartbeat_timeout=timedelta(seconds=limits.heartbeat_timeout_seconds),
                schedule_to_close_timeout=timedelta(seconds=limits.give_up_after_seconds),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=limits.retry_after_seconds),
                    backoff_coefficient=RETRY_BACKOFF,
                    maximum_interval=timedelta(seconds=limits.max_retry_wait_seconds),
                    non_retryable_error_types=list(steps.NOT_RETRIED),
                ),
            )
        except ActivityError as failure:
            workflow.logger.warning(
                "chat %s did not follow up on research run %s: %s",
                follow_up.chat_id,
                follow_up.run_id,
                reason_shown(
                    failure, steps.FOLLOW_UP_TEXTS, worded_types=steps.WORDED_BY_THE_ACTIVITY
                ),
            )
            cause = not_taken_cause(failure)
            if cause is not None:
                await self._tell_the_chat(FollowUpNotTaken(follow_up, cause))

    async def _tell_the_chat(self, not_taken: FollowUpNotTaken) -> None:
        limits = policy.NOT_TAKEN
        try:
            await workflow.execute_activity(
                steps.RECORD_FOLLOW_UP_NOT_TAKEN,
                not_taken,
                start_to_close_timeout=timedelta(seconds=limits.attempt_timeout_seconds),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=limits.retry_after_seconds),
                    maximum_attempts=limits.attempts,
                ),
            )
        except ActivityError as failure:
            workflow.logger.warning(
                "chat %s was not told it will not hear about research run %s: %s",
                not_taken.follow_up.chat_id,
                not_taken.follow_up.run_id,
                reason_shown(
                    failure, steps.FOLLOW_UP_TEXTS, worded_types=steps.WORDED_BY_THE_ACTIVITY
                ),
            )


def not_taken_cause(failure: ActivityError) -> str | None:
    """What the chat is told of a follow-up that ended with ``failure``, or ``None``.

    ``None`` for a follow-up with nothing to follow up on (there is no chat to tell, or no
    outcome yet) and for one that was cancelled.
    """
    if is_cancelled_exception(failure):
        return None
    reason = failure.__cause__
    if isinstance(reason, TimeoutError):
        return steps.NOT_TAKEN_IN_TIME
    if isinstance(reason, ApplicationError):
        if reason.type == steps.NOTHING_TO_FOLLOW_UP:
            return None
        return steps.NOT_TAKEN_BECAUSE.get(reason.type or "", steps.NOT_TAKEN_UNEXPECTEDLY)
    return steps.NOT_TAKEN_UNEXPECTEDLY
