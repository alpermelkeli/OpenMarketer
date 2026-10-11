"""``run_graph_with_actions``: the loop an agent's workflow runs its graph with.

The graph runs inside the activity ``run_graph_step`` until it finishes or asks
for an action. The driver then runs the action as the activity ``run_action``,
under that action's own timeouts and retries, and takes the next step with what
came of it, until a step reports that the graph finished. The graph decides
what is needed; this loop and Temporal decide what runs and how often it is
tried again.

The driver is the one place an action is started, so what has to stand between
a request and its side effect stands here:

- A name that is not in ``actions`` is refused in the workflow itself. Nothing
  runs, and the model's text never becomes an activity.
- A run may ask for ``policy.max_actions`` actions and no more: each one adds to
  its history.
- An action that failed for good is told to the graph as a failed outcome, so
  the agent decides what to do. Only a defect of the agent ends the run
  (``RUN_ENDING_ACTION_FAILURES``).

What the graph is told of a failure is the message of the error the action's handler worded,
or a fixed text (``ACTION_TEXTS``) for a timeout or an error nobody worded, cut to
``MAX_ERROR_CHARACTERS``. It goes into the model's context and into the next step's input, so a
handler words its failures for the model to read: it does not put in one the reply text of a
service, or anything the model must not see. Any ``ApplicationError`` of ``run_action`` is
told this way, whatever its type, including one the SDK makes itself (an activity that is not
registered on the worker, say); the cut is the limit.

Everything that enters the history is an identifier, a name or small JSON, and the activities
refuse what is larger (``MAX_STEP_BYTES``, ``MAX_RESULT_BYTES``). A failure's message is one
somebody chose; the driver never takes wording from an error that nobody worded.

What it raises, which an agent's workflow that records how its runs end has to catch all of:
``ActivityError`` when a step fails or an action ends the run, and ``ApplicationError`` when
the driver refuses a request itself (an action the workflow does not list, too many
actions), which is not an ``ActivityError``. An ``except (ActivityError,
asyncio.CancelledError)`` as the analyzer's workflow has would let a refusal pass and leave the
run unrecorded; catch ``ApplicationError`` as well. Cancellation passes through. The driver
records nothing on a run and does not forget its checkpoints: what a run owes when it ends is
the agent's workflow.

``ActionPolicy`` (core's, made by ``Agent.action``) and ``GraphRunPolicy`` check their values
when they are made. They are constants of the agent's modules and not a workflow's input: a
``ValueError`` while Temporal decodes a workflow's input fails the workflow task, which is
tried again for ever.

This is workflow code, loaded by the sandbox. An agent's workflow module may import it plainly
or inside ``workflow.unsafe.imports_passed_through()``; both work, and the tests run both.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, is_cancelled_exception

with workflow.unsafe.imports_passed_through():
    from openmarketer_core.agent_runtime.actions import (
        ActionOutcome,
        ActionPolicy,
        ActionRequest,
        UnknownAction,
    )
    from openmarketer_core.agent_runtime.steps import GraphRunRef, StepOutcome
    from openmarketer_worker.agent_runtime.vocabulary import (
        ACTION_TEXTS,
        MAX_ERROR_CHARACTERS,
        RETRY_BACKOFF,
        RUN_ACTION,
        RUN_ENDING_ACTION_FAILURES,
        RUN_GRAPH_STEP,
        STEP_REFUSED,
        TOO_MANY_ACTIONS,
        UNKNOWN_ACTION,
        GraphRunPolicy,
        StepInput,
    )
    from openmarketer_worker.agent_runtime.workflow_support import reason_shown


async def run_graph_with_actions(
    run: GraphRunRef, *, actions: Mapping[str, ActionPolicy], policy: GraphRunPolicy
) -> dict[str, Any]:
    """Run the graph of ``run`` and the actions it asks for; the graph's final output.

    ``actions`` is the allowlist: the name of each action the graph may ask for,
    with the limits it runs under. It is plain data that the agent's workflow
    module holds, so the sandbox can load it.
    """
    resume: ActionOutcome | None = None
    asked = 0
    while True:
        step = await _take_step(StepInput(run=run, resume=resume), policy)
        if step.output is not None:
            return step.output
        request = step.action
        if request is None:
            # ``StepOutcome`` cannot be made so. An assertion here would fail the workflow task,
            # which is tried again for ever.
            raise ApplicationError(
                "a step reported neither an output nor an action",
                type=STEP_REFUSED,
                non_retryable=True,
            )
        limits = actions.get(request.name)
        if limits is None:
            raise ApplicationError(
                str(UnknownAction(request.name)), type=UNKNOWN_ACTION, non_retryable=True
            )
        if asked >= policy.max_actions:
            raise ApplicationError(
                f"the run asked for more than {policy.max_actions} actions",
                type=TOO_MANY_ACTIONS,
                non_retryable=True,
            )
        asked += 1
        resume = await _take_action(request, limits)


async def _take_step(step: StepInput, policy: GraphRunPolicy) -> StepOutcome:
    return await workflow.execute_activity(
        RUN_GRAPH_STEP,
        step,
        result_type=StepOutcome,
        start_to_close_timeout=timedelta(seconds=policy.attempt_timeout_seconds),
        heartbeat_timeout=timedelta(seconds=policy.heartbeat_timeout_seconds),
        retry_policy=RetryPolicy(
            initial_interval=timedelta(seconds=policy.retry_after_seconds),
            backoff_coefficient=RETRY_BACKOFF,
            maximum_attempts=policy.max_attempts,
            non_retryable_error_types=[STEP_REFUSED],
        ),
    )


async def _take_action(request: ActionRequest, limits: ActionPolicy) -> ActionOutcome:
    heartbeat_seconds = limits.heartbeat_timeout_seconds
    heartbeat_timeout = None if heartbeat_seconds is None else timedelta(seconds=heartbeat_seconds)
    try:
        return await workflow.execute_activity(
            RUN_ACTION,
            request,
            result_type=ActionOutcome,
            start_to_close_timeout=timedelta(seconds=limits.attempt_timeout_seconds),
            heartbeat_timeout=heartbeat_timeout,
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=limits.retry_after_seconds),
                backoff_coefficient=RETRY_BACKOFF,
                maximum_attempts=limits.max_attempts,
                non_retryable_error_types=list(RUN_ENDING_ACTION_FAILURES),
            ),
        )
    except ActivityError as failure:
        if is_cancelled_exception(failure) or _ends_the_run(failure):
            raise
        return ActionOutcome(
            action_id=request.action_id,
            succeeded=False,
            result={},
            error=_cut(reason_shown(failure, ACTION_TEXTS)),
        )


def _ends_the_run(failure: ActivityError) -> bool:
    cause = failure.cause
    return isinstance(cause, ApplicationError) and cause.type in RUN_ENDING_ACTION_FAILURES


def _cut(text: str) -> str:
    """``text``, or its start and "..." when it is longer than the graph is told of a failure."""
    if len(text) <= MAX_ERROR_CHARACTERS:
        return text
    return text[: MAX_ERROR_CHARACTERS - 3] + "..."
