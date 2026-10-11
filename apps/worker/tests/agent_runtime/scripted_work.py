"""What the agent runtime's tests run: a scripted graph, scripted actions and a way to read history.

The graph asks for the actions of a plan one after another, through core's
``request_action`` and ``GraphThread`` on the real checkpoint store, and stops
at the first action that failed: that is "the agent's own decision" in these
tests. The actions are handlers that do what a test tells them and remember
what they were asked. No model is involved.
"""

import asyncio
import base64
import json
import operator
import re
import threading
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from typing import Annotated, Any, TypedDict

import pytest
from google.protobuf.json_format import MessageToDict
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict
from scripted_agent import ScriptedAgent, ScriptedRun, WritesToTheDatabase
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle, WorkflowHistory
from temporalio.worker import Worker

from openmarketer_core.agent_runtime.actions import ActionDefinition, ActionOutcome
from openmarketer_core.agent_runtime.langgraph_steps import (
    CompiledGraph,
    GraphThread,
    request_action,
)
from openmarketer_core.agent_runtime.steps import GraphRunRef, StepOutcome
from openmarketer_core.agent_runtime.threads import thread_id
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_worker.agent_runtime.activities import RuntimeActivities
from openmarketer_worker.agent_runtime.routes import (
    ActionContext,
    ActionHandler,
    ActionRoute,
    ActionRoutes,
)
from openmarketer_worker.agent_runtime.vocabulary import ActionPolicy, GraphRunPolicy

KIND = "scripted-agent"

# Limits short enough for a test and long enough for a slow machine. The dev server waits at
# least a second before a retry, whatever the interval asked for.
STEP_LIMITS = GraphRunPolicy(
    attempt_timeout_seconds=30,
    heartbeat_timeout_seconds=15,
    max_attempts=2,
    retry_after_seconds=0.2,
)
ACTION_LIMITS = ActionPolicy(attempt_timeout_seconds=30, max_attempts=2, retry_after_seconds=0.2)
ALLOWED = {"send_email": ACTION_LIMITS, "publish": ACTION_LIMITS}


class SendEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: str


class Publish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    post: int


class Anything(BaseModel):
    """What the graph asks with: any payload, so a test can ask for what no model fits."""

    model_config = ConfigDict(extra="allow")


@dataclass(frozen=True)
class Ask:
    """One action of a graph's plan."""

    name: str
    payload: dict[str, Any]


def some_run() -> GraphRunRef:
    return GraphRunRef(kind=KIND, run_id=uuid.uuid4())


def thread_of(run: GraphRunRef) -> str:
    return thread_id(run.kind, run.run_id)


# ------------------------------------------------------------------ the scripted graph
class State(TypedDict):
    plan: list[dict[str, Any]]
    answers: Annotated[list[dict[str, Any]], operator.add]


def asks_for_the_next_action(state: State) -> dict[str, Any]:
    ask = state["plan"][len(state["answers"])]
    outcome = request_action(ask["name"], Anything(**ask["payload"]))
    return {"answers": [asdict(outcome)]}


def goes_on(state: State) -> str:
    answers = state["answers"]
    if answers and not answers[-1]["succeeded"]:
        return END
    return "ask" if len(answers) < len(state["plan"]) else END


def compiled(checkpoints: RunCheckpoints) -> CompiledGraph:
    builder = StateGraph(State)
    builder.add_node("ask", asks_for_the_next_action)
    builder.add_conditional_edges(START, goes_on, ["ask", END])
    builder.add_conditional_edges("ask", goes_on, ["ask", END])
    return builder.compile(checkpointer=checkpoints.store)


class ScriptedGraph:
    """An ``AgentGraph`` that asks for the actions of ``plan`` and remembers how it was advanced."""

    def __init__(self, plan: list[Ask], *, dies_after_applying: int = 0) -> None:
        self._plan = plan
        self._deaths_left = dies_after_applying
        self.advanced: list[ActionOutcome | None] = []  # the outcome each step was given

    def advance(
        self, run: GraphRunRef, checkpoints: RunCheckpoints, resume: ActionOutcome | None
    ) -> StepOutcome:
        self.advanced.append(resume)
        initial = {"plan": [asdict(ask) for ask in self._plan], "answers": []}
        step = GraphThread(compiled(checkpoints), checkpoints).advance(
            initial=initial, resume=resume
        )
        if resume is not None and self._deaths_left:
            self._deaths_left -= 1
            # The outcome is applied and stored; what the step found out is lost with the worker.
            raise RuntimeError("the worker died after the outcome was applied")
        return step


class RaisingGraph:
    """An ``AgentGraph`` that fails with ``error`` whenever it is advanced."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.advanced = 0

    def advance(
        self, run: GraphRunRef, checkpoints: RunCheckpoints, resume: ActionOutcome | None
    ) -> StepOutcome:
        self.advanced += 1
        raise self._error


class BlockingGraph:
    """An ``AgentGraph`` whose step does not return until it is released: a graph that works."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def advance(
        self, run: GraphRunRef, checkpoints: RunCheckpoints, resume: ActionOutcome | None
    ) -> StepOutcome:
        self.started.set()
        self.release.wait(timeout=60)
        return StepOutcome.finished({})


# ------------------------------------------------------------------ scripted actions
@dataclass
class ScriptedAction:
    """A handler that takes the next of ``outcomes`` for each call: an error to raise, or a result.

    With no outcome left it succeeds with an empty result.
    """

    outcomes: list[Exception | Mapping[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.calls: list[tuple[BaseModel, ActionContext]] = []

    def __call__(self, payload: BaseModel, context: ActionContext) -> Mapping[str, Any]:
        self.calls.append((payload, context))
        outcome = self.outcomes.pop(0) if self.outcomes else {}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @property
    def action_ids(self) -> list[str]:
        return [context.action_id for _, context in self.calls]


class BlockingAction:
    """A handler that does not return until it is released: an action that is still running."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, payload: BaseModel, context: ActionContext) -> Mapping[str, Any]:
        self.started.set()
        self.release.wait(timeout=60)
        return {}


def route(
    name: str, model: type[BaseModel], handler: ActionHandler, policy: ActionPolicy = ACTION_LIMITS
) -> ActionRoute:
    return ActionRoute(ActionDefinition(name, model), handler, policy)


def routes_of(send: ActionHandler, publish: ActionHandler) -> ActionRoutes:
    return ActionRoutes([route("send_email", SendEmail, send), route("publish", Publish, publish)])


SEND_THEN_PUBLISH = [Ask("send_email", {"to": "ada@example.com"}), Ask("publish", {"post": 7})]


# ------------------------------------------------------------------ running workflows
@dataclass
class Execution:
    client: Client
    task_queue: str

    async def start(
        self,
        run: GraphRunRef,
        *,
        actions: dict[str, ActionPolicy] | None = None,
        policy: GraphRunPolicy = STEP_LIMITS,
    ) -> WorkflowHandle:
        scripted = ScriptedRun(
            run=run, actions=ALLOWED if actions is None else actions, policy=policy
        )
        return await self.client.start_workflow(
            ScriptedAgent.run, scripted, id=f"scripted-{run.run_id}", task_queue=self.task_queue
        )

    async def finish(self, run: GraphRunRef, **limits: Any) -> dict[str, Any]:
        return await (await self.start(run, **limits)).result()

    async def fail(self, run: GraphRunRef, **limits: Any) -> BaseException:
        """What the run failed with: the failure that ended the workflow."""
        with pytest.raises(WorkflowFailureError) as failed:
            await self.finish(run, **limits)
        assert failed.value.__cause__ is not None
        return failed.value.__cause__

    async def history(self, run: GraphRunRef) -> WorkflowHistory:
        return await self.client.get_workflow_handle(f"scripted-{run.run_id}").fetch_history()


@asynccontextmanager
async def serving_runs(
    client: Client, task_queue: str, activities: RuntimeActivities
) -> AsyncIterator[Execution]:
    """A worker with the runtime's activities and the workflow of ``scripted_agent``."""
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[ScriptedAgent, WritesToTheDatabase],
        activities=[activities.run_graph_step, activities.run_action],
    )
    async with worker:
        yield Execution(client, task_queue)


async def happened(event: threading.Event, *, within: float = 20) -> None:
    """Returns once a thread has set ``event``; fails the test when it does not in time."""
    reached = await asyncio.get_running_loop().run_in_executor(None, event.wait, within)
    assert reached, "the action did not start"


# ------------------------------------------------------------------ reading history
def readable(history: WorkflowHistory) -> str:
    """The history as the Temporal UI shows it: payloads are stored encoded, so decode them."""
    document = json.dumps([MessageToDict(event) for event in history.events])
    payloads = [
        base64.b64decode(data).decode(errors="replace")
        for data in re.findall(r'"data": "([A-Za-z0-9+/=]*)"', document)
    ]
    return "\n".join([document, *payloads])


@dataclass(frozen=True)
class Exchange:
    """What one activity was given and what it returned, decoded."""

    activity: str
    given: Any
    returned: Any


def exchanges(history: WorkflowHistory) -> list[Exchange]:
    """The input and the result of every activity that completed, in the order it was scheduled."""
    scheduled: dict[int, tuple[str, Any]] = {}
    results: dict[int, Any] = {}
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            attributes = event.activity_task_scheduled_event_attributes
            (given,) = attributes.input.payloads
            scheduled[event.event_id] = (attributes.activity_type.name, json.loads(given.data))
        elif event.HasField("activity_task_completed_event_attributes"):
            attributes = event.activity_task_completed_event_attributes
            (returned,) = attributes.result.payloads
            results[attributes.scheduled_event_id] = json.loads(returned.data)
    return [
        Exchange(activity, given, results.get(event_id))
        for event_id, (activity, given) in scheduled.items()
    ]


def failure_events(history: WorkflowHistory) -> str:
    """The failures the history holds (those of activities and of the workflow), as text."""
    failed = [
        MessageToDict(event)
        for event in history.events
        if event.HasField("activity_task_failed_event_attributes")
        or event.HasField("workflow_execution_failed_event_attributes")
    ]
    return json.dumps(failed)
