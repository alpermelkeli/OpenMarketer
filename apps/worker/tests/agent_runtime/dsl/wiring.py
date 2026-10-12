"""The greeter's worker side: what it reaches, its action's handler, its activity, and the wiring.

Only the worker's process loads this module. ``GreeterWorld`` stands for the outside world one
wiring is given: the service a handler reaches, whom each run greets (what a real agent reads
from its run's row), and what the activity its hooks call was asked to note. It is made by each
test and handed to ``wire``, so nothing here is module-level state that a test could leave behind.
"""

import asyncio
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from temporalio import activity

from dsl.definitions import LIMITS_OF_THE_RUN, NOTE, Greet, GreeterContext, Limits, Note, agent
from dsl.graph import final_result, initial_state
from dsl.workflow import (
    Greeter,
    GreeterInItsOwnWords,
    GreeterOfARequest,
    GreeterWithLimitsOfItsOwn,
)
from dsl.workflow_passthrough import GreeterPassedThrough
from openmarketer_core.agent_runtime.langgraph_agent import AgentSteps
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_worker import AgentRegistration
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess
from openmarketer_worker.agent_runtime.agent_worker import AgentWorker
from openmarketer_worker.agent_runtime.routes import ActionContext

TASK_QUEUE = "dsl-greeter"
DEFAULT_SUBJECT = "Ada"


@dataclass
class GreetingService:
    """The service a greeting is sent through: every call it got, and what it does next.

    A handler passes it the action's id as the key of its side effect, as a handler of a real
    service must.
    """

    failures: list[Exception] = field(default_factory=list)
    calls: list[tuple[Greet, str]] = field(default_factory=list)  # the payload, and the key
    holds: bool = False  # whether the next call waits until it is released

    def __post_init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def greet(self, payload: Greet, *, key: str) -> str:
        self.calls.append((payload, key))
        if self.holds:
            self.started.set()
            self.release.wait(timeout=60)
        if self.failures:
            raise self.failures.pop(0)
        return f"hello, {payload.name}"


@dataclass
class Hold:
    """A note that waits to be let go, so that a test can do something while a hook is running."""

    arrived: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class GreeterWorld:
    """What one wiring of the greeter reaches, and what a test reads back of it."""

    service: GreetingService = field(default_factory=GreetingService)
    subjects: dict[uuid.UUID, str] = field(default_factory=dict)  # whom each run greets
    notes: list[Note] = field(default_factory=list)  # what the hooks had noted
    holds: dict[str, Hold] = field(default_factory=dict)  # by the step whose note waits
    refusals: dict[str, Exception] = field(default_factory=dict)  # by the step whose note fails
    limits: Limits = Limits(attempt_timeout_seconds=40)  # what the settings say of a step
    released: list[GraphRunRef] = field(default_factory=list)  # runs the agent was told ended
    release_fails_with: Exception | None = None

    def hold_the_note_of(self, step: str) -> Hold:
        self.holds[step] = Hold()
        return self.holds[step]


def wire(world: GreeterWorld, checkpoints: CheckpointAccess) -> AgentRegistration:
    """The greeter set up on ``world``, as the wiring of a real agent sets it up for its process."""
    worker = AgentWorker(agent, task_queue=TASK_QUEUE)

    @worker.handler(Greet)
    def greet(payload: Greet, context: ActionContext) -> Mapping[str, Any]:
        return {"text": world.service.greet(payload, key=context.action_id)}

    @worker.run_ended
    def release(run: GraphRunRef) -> None:
        world.released.append(run)
        if world.release_fails_with is not None:
            raise world.release_fails_with

    @activity.defn(name=NOTE)
    async def note(entry: Note) -> None:
        hold = world.holds.get(entry.step)
        if hold is not None:
            hold.arrived.set()
            await asyncio.wait_for(hold.release.wait(), timeout=60)
        if entry.step in world.refusals:
            raise world.refusals[entry.step]
        world.notes.append(entry)

    @activity.defn(name=LIMITS_OF_THE_RUN)
    async def limits_of_the_run() -> Limits:
        return world.limits

    steps = AgentSteps(
        agent,
        initial_state=initial_state,
        finish=lambda run, final: final_result(final),
        recursion_limit=20,
        context=lambda run, held: GreeterContext(
            subject=world.subjects.get(run.run_id, DEFAULT_SUBJECT)
        ),
    )
    return worker.registration(
        checkpoints=checkpoints,
        graph=steps,
        workflows=[
            Greeter,
            GreeterPassedThrough,
            GreeterWithLimitsOfItsOwn,
            GreeterOfARequest,
            GreeterInItsOwnWords,
        ],
        activities=[note, limits_of_the_run],
    )
