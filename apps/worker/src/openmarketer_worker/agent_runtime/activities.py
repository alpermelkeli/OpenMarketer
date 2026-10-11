"""The two activities every agent that runs a graph with actions registers.

``run_graph_step`` takes one step of the agent's graph on the run's thread:
from where it stands until it finishes or asks for an action. ``run_action``
runs one action the graph asked for, through the handler of its route. Both
are the generic half of the agent runtime (``driver.py`` is the workflow's);
the agent hands in its graph, its routes and its limits when the worker starts.

They hold no rule of their own. A step is core's ``GraphThread``, behind the
agent's ``AgentGraph``. An action is checked against the catalog of the routes
before anything runs, because its name and payload are text the model may have
shaped, and what the catalog says names the action and the fields at fault,
never their values. Every failure is raised with a type that says whether
another attempt is worth its cost and a message that may be stored and shown
(``activity_support.worded_failures``); the original error is logged.

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
thread. Nothing here forgets a run's checkpoints when the run ends: that is the
agent's own activity at the end of its run (``activity_support.forget_quietly``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel
from temporalio import activity

from openmarketer_core.agent_runtime.actions import (
    ActionOutcome,
    ActionRequest,
    InvalidActionPayload,
    UnknownAction,
)
from openmarketer_core.agent_runtime.steps import GraphRunRef, StepError, StepOutcome
from openmarketer_core.agent_runtime.threads import thread_id
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.graph_checkpoints.postgres import AttemptInProgress
from openmarketer_worker.agent_runtime.activity_support import (
    CheckpointAccess,
    failure,
    run_with_heartbeats,
    worded_failures,
)
from openmarketer_worker.agent_runtime.routes import ActionContext, ActionRoute, ActionRoutes
from openmarketer_worker.agent_runtime.vocabulary import (
    ACTION_RESULT_TOO_LARGE,
    ACTION_TEXTS,
    ATTEMPT_STILL_RUNNING,
    EARLIER_ATTEMPT_RUNNING,
    INVALID_ACTION_PAYLOAD,
    INVALID_ACTION_RESULT,
    MAX_RESULT_BYTES,
    RUN_ACTION,
    RUN_GRAPH_STEP,
    RUN_TEXTS,
    STEP_REFUSED,
    UNKNOWN_ACTION,
    GraphRunPolicy,
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
    """

    def advance(
        self, run: GraphRunRef, checkpoints: RunCheckpoints, resume: ActionOutcome | None
    ) -> StepOutcome: ...


class RuntimeActivities:
    def __init__(
        self,
        checkpoints: CheckpointAccess,
        graph: AgentGraph,
        routes: ActionRoutes,
        policy: GraphRunPolicy,
    ) -> None:
        self._checkpoints = checkpoints
        self._graph = graph
        self._routes = routes
        self._policy = policy

    @activity.defn(name=RUN_GRAPH_STEP)
    async def run_graph_step(self, step: StepInput) -> StepOutcome:
        """Take one step of the run's graph, and say whether it finished or waits for an action.

        The graph's work blocks for as long as it takes, so it runs in a thread
        while this coroutine sends heartbeats.
        """
        subject = f"{step.run.kind} run {step.run.run_id}"
        with worded_failures(RUN_TEXTS, subject=subject):
            return await run_with_heartbeats(
                lambda: self._advance(step, subject),
                every_seconds=self._policy.heartbeat_every_seconds,
                subject=subject,
            )

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
            return ActionOutcome(
                action_id=request.action_id,
                succeeded=True,
                result=_small_json(request.name, result),
                error="",
            )

    def _advance(self, step: StepInput, subject: str) -> StepOutcome:
        thread = thread_id(step.run.kind, step.run.run_id)
        try:
            with self._checkpoints.of_attempt(thread) as checkpoints:
                return self._graph.advance(step.run, checkpoints, step.resume)
        except AttemptInProgress:
            logger.info("%s: an earlier attempt is still running", subject)
            raise failure(ATTEMPT_STILL_RUNNING, EARLIER_ATTEMPT_RUNNING) from None
        except StepError as e:
            logger.warning("%s: its graph cannot take the step: %s", subject, e)
            raise failure(STEP_REFUSED, str(e), non_retryable=True) from None

    def _validated(self, request: ActionRequest, subject: str) -> BaseModel:
        try:
            return self._routes.catalog.validate(request)
        except UnknownAction as e:
            logger.warning("%s was refused: %s", subject, e)
            raise failure(UNKNOWN_ACTION, str(e), non_retryable=True) from None
        except InvalidActionPayload as e:
            logger.warning("%s was refused: %s", subject, e)
            raise failure(INVALID_ACTION_PAYLOAD, str(e), non_retryable=True) from None

    async def _run_handler(
        self, route: ActionRoute, payload: BaseModel, context: ActionContext, subject: str
    ) -> Mapping[str, Any]:
        def work() -> Mapping[str, Any]:
            return route.handler(payload, context)

        every_seconds = route.policy.heartbeat_every_seconds
        if every_seconds is None:
            return await asyncio.to_thread(work)
        return await run_with_heartbeats(work, every_seconds=every_seconds, subject=subject)


def _small_json(action_name: str, result: Mapping[str, Any]) -> dict[str, Any]:
    """``result`` as the dict an outcome carries, or a failure for good when it cannot be.

    The outcome is kept in the workflow history, so it is bounded by what the
    history stores of it: its JSON as the default converter writes it.
    """
    try:
        plain = dict(result)
        stored = json.dumps(plain, separators=(",", ":"))
    except (TypeError, ValueError):
        raise failure(
            INVALID_ACTION_RESULT,
            f"the result of action {action_name} is not JSON",
            non_retryable=True,
        ) from None
    # The converter's JSON is ASCII, so its length in characters is its length in bytes.
    if len(stored) > MAX_RESULT_BYTES:
        raise failure(
            ACTION_RESULT_TOO_LARGE,
            f"the result of action {action_name} is over {MAX_RESULT_BYTES} bytes",
            non_retryable=True,
        )
    return plain
