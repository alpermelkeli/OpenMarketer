"""The three activities every agent that runs a graph with actions registers.

``run_graph_step`` takes one step of the agent's graph on the run's thread:
from where it stands until it finishes or asks for an action. ``run_action``
runs one action the graph asked for, through the handler of its route.
``forget_run`` removes the checkpoints of a run that is over. They are the generic
half of the agent runtime (``driver.py`` is the workflow's); the agent hands in its
graph and its routes when the worker starts.

They hold no rule of their own. A step is core's ``GraphThread``, behind the
agent's ``AgentGraph``. An action is checked against the catalog of the routes
before anything runs, because its name and payload are text the model may have
shaped, and what the catalog says names the action and the fields at fault,
never their values. A graph's own defects (a node that asks with a model no action was
declared for, a route that chooses a node it was not given) end the run at once, like a thread
that refuses the step: another attempt would meet them again. Every failure is raised with a
type that says whether another attempt is worth its cost and a message that may be stored and
shown (``activity_support.worded_failures``, in the runtime's own words: ``RUN_TEXTS`` for a
step, ``ACTION_TEXTS`` for an action); the original error is logged.

What an activity returns enters the workflow history, so both refuse what is too large or is
not JSON, for good: a step's outcome over ``MAX_STEP_BYTES`` and an action's over
``MAX_RESULT_BYTES``. A retry could not make either smaller, and an action's result is
checked after the handler ran, so a retry would run it again.

Activities run at least once. A step is safe to run again: an outcome that an
earlier attempt applied is not applied twice (``GraphThread.advance``). An
action is not, by itself: its handler gets ``ActionContext.action_id``, the
same each time the same step is attempted again, to make its side effect once.
A thread cannot be stopped, so an attempt that timed out or was cancelled may
still be running its handler when the next one starts: the key is what keeps
the two from doing the work twice.

Checkpoints: an attempt at a step holds the run's thread for as long as its
graph works, even after Temporal has given up on it, and a second attempt is
refused and retried (``ATTEMPT_STILL_RUNNING``), never allowed to write the same
thread. A step and an action forget nothing: when the run ends, the agent's workflow calls
``forget_run`` (``activity_support.forget_quietly``, so the activity never fails for a
database that is down), whether the run finished, failed or was cancelled.

How often the activities heartbeat is not set here: it is a third of the heartbeat timeout
the workflow scheduled them with (``activity_support.run_in_thread``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel
from temporalio import activity
from temporalio.exceptions import ApplicationError

from openmarketer_core.agent_runtime.actions import (
    ActionOutcome,
    ActionRequest,
    InvalidActionPayload,
    NotAnAction,
    UnknownAction,
)
from openmarketer_core.agent_runtime.agent import InvalidRouteChoice
from openmarketer_core.agent_runtime.steps import GraphRunRef, StepError, StepOutcome
from openmarketer_core.agent_runtime.threads import thread_id
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.graph_checkpoints.postgres import AttemptInProgress
from openmarketer_worker.agent_runtime.activity_support import (
    CheckpointAccess,
    NotStorable,
    forget_quietly,
    run_in_thread,
    stored_size,
    worded_failures,
)
from openmarketer_worker.agent_runtime.routes import ActionContext, ActionRoute, ActionRoutes
from openmarketer_worker.agent_runtime.vocabulary import (
    ACTION_RESULT_TOO_LARGE,
    ACTION_TEXTS,
    ATTEMPT_STILL_RUNNING,
    EARLIER_ATTEMPT_RUNNING,
    FORGET_RUN,
    INVALID_ACTION_PAYLOAD,
    INVALID_ACTION_RESULT,
    MAX_RESULT_BYTES,
    MAX_STEP_BYTES,
    RUN_ACTION,
    RUN_GRAPH_STEP,
    RUN_TEXTS,
    STEP_REFUSED,
    UNKNOWN_ACTION,
    StepInput,
)

logger = logging.getLogger(__name__)


class AgentGraph(Protocol):
    """An agent's graph, as the runtime takes a step of it.

    The agent's wiring implements it with its compiled graph and core's
    ``GraphThread``: a run's first step starts the thread from an initial state
    built from the run's identifiers, a later one continues it with the outcome
    of the action it waited for. It blocks, and runs in a thread. A failure it
    worded (an ``ApplicationError``) passes as it is; any other is logged and
    reported as unexpected.

    What ``advance`` returns is what enters the workflow history, and the
    runtime refuses an outcome over ``MAX_STEP_BYTES``. So it is a small projection
    of the graph's state, never the state: ``GraphThread.advance`` hands over the whole
    final state, with whatever conversation a model has had in it, and the wiring keeps
    only what the workflow and the run's reader need.
    """

    def advance(
        self, run: GraphRunRef, checkpoints: RunCheckpoints, resume: ActionOutcome | None
    ) -> StepOutcome: ...


class RuntimeActivities:
    def __init__(
        self, checkpoints: CheckpointAccess, graph: AgentGraph, routes: ActionRoutes
    ) -> None:
        self._checkpoints = checkpoints
        self._graph = graph
        self._routes = routes

    @activity.defn(name=RUN_GRAPH_STEP)
    async def run_graph_step(self, step: StepInput) -> StepOutcome:
        """Take one step of the run's graph, and say whether it finished or waits for an action.

        The graph's work blocks for as long as it takes, so it runs in a thread
        while this coroutine sends heartbeats.
        """
        subject = f"{step.run.kind} run {step.run.run_id}"
        with worded_failures(RUN_TEXTS, subject=subject):
            outcome = await run_in_thread(lambda: self._advance(step, subject), subject=subject)
            return _within_history_bounds(outcome)

    @activity.defn(name=RUN_ACTION)
    async def run_action(self, request: ActionRequest) -> ActionOutcome:
        """Run the action the graph asked for, and answer with its small result.

        The request is checked against the catalog first; one that is not allowed
        or does not fit its model fails for good and runs nothing. A result that
        is not small JSON fails for good as well: the action has run by then, and
        another attempt would run it again.
        """
        subject = f"action {request.action_id}"
        with worded_failures(ACTION_TEXTS, subject=subject):
            payload = self._validated(request, subject)
            route = self._routes.route(request.name)
            result = await self._run_handler(
                route, payload, ActionContext(request.action_id), subject
            )
            return _outcome_within_bounds(request, result)

    @activity.defn(name=FORGET_RUN)
    async def forget_run(self, run: GraphRunRef) -> None:
        """Remove the checkpoints of a run that is over, however it ended.

        Forgetting never fails the activity for a database that is down: the run's outcome is
        recorded by then, and the rows are left to a cleanup. A run whose kind cannot name a
        thread is refused for good, as a step is.
        """
        subject = f"{run.kind} run {run.run_id}"
        with worded_failures(RUN_TEXTS, subject=subject):
            thread = _thread_of(run)
            await asyncio.to_thread(forget_quietly, self._checkpoints, thread, subject=subject)

    def _advance(self, step: StepInput, subject: str) -> StepOutcome:
        thread = _thread_of(step.run)
        try:
            with self._checkpoints.of_attempt(thread) as checkpoints:
                return self._graph.advance(step.run, checkpoints, step.resume)
        except AttemptInProgress:
            logger.info("%s: an earlier attempt is still running", subject)
            raise ApplicationError(EARLIER_ATTEMPT_RUNNING, type=ATTEMPT_STILL_RUNNING) from None
        except (StepError, NotAnAction, InvalidRouteChoice) as e:
            # The messages of all three name what is wrong and never a value the graph held.
            logger.warning("%s: its graph cannot take the step: %s", subject, e)
            raise ApplicationError(str(e), type=STEP_REFUSED, non_retryable=True) from None

    def _validated(self, request: ActionRequest, subject: str) -> BaseModel:
        try:
            return self._routes.catalog.validate(request)
        except UnknownAction as e:
            logger.warning("%s was refused: %s", subject, e)
            raise ApplicationError(str(e), type=UNKNOWN_ACTION, non_retryable=True) from None
        except InvalidActionPayload as e:
            logger.warning("%s was refused: %s", subject, e)
            raise ApplicationError(
                str(e), type=INVALID_ACTION_PAYLOAD, non_retryable=True
            ) from None

    async def _run_handler(
        self, route: ActionRoute, payload: BaseModel, context: ActionContext, subject: str
    ) -> Mapping[str, Any]:
        def work() -> Mapping[str, Any]:
            return route.handler(payload, context)

        return await run_in_thread(work, subject=subject)


def _thread_of(run: GraphRunRef) -> str:
    try:
        return thread_id(run.kind, run.run_id)
    except ValueError:
        # The kind comes from the agent's workflow: it is a defect, and another attempt
        # would meet it again. Its text is not repeated, as it names the kind.
        raise ApplicationError(
            "the kind of this run cannot name a thread: it is lower-case letters, digits "
            "and hyphens",
            type=STEP_REFUSED,
            non_retryable=True,
        ) from None


def _within_history_bounds(outcome: StepOutcome) -> StepOutcome:
    """``outcome`` itself, or a failure for good when the history could not hold it."""
    try:
        size = stored_size(outcome)
    except NotStorable:
        raise ApplicationError(
            "the outcome of the step is not JSON", type=STEP_REFUSED, non_retryable=True
        ) from None
    if size > MAX_STEP_BYTES:
        raise ApplicationError(
            f"the outcome of the step is over {MAX_STEP_BYTES} bytes: "
            "a graph reports a small projection of its state",
            type=STEP_REFUSED,
            non_retryable=True,
        )
    return outcome


def _outcome_within_bounds(request: ActionRequest, result: Mapping[str, Any]) -> ActionOutcome:
    """The outcome of a successful action, or a failure for good when the history cannot hold it.

    The outcome is built and measured here, as the data converter will write it, because the
    converter writes it only when the activity returns, after the handler ran: a result it
    cannot write would fail the activity then, and be tried again, and run the handler again.
    """
    name = request.name
    try:
        outcome = ActionOutcome(request.action_id, succeeded=True, result=dict(result), error="")
        size = stored_size(outcome)
    except (NotStorable, TypeError, ValueError):
        raise ApplicationError(
            f"the result of action {name} is not JSON",
            type=INVALID_ACTION_RESULT,
            non_retryable=True,
        ) from None
    if size > MAX_RESULT_BYTES:
        raise ApplicationError(
            f"the result of action {name} is over {MAX_RESULT_BYTES} bytes",
            type=ACTION_RESULT_TOO_LARGE,
            non_retryable=True,
        )
    return outcome
