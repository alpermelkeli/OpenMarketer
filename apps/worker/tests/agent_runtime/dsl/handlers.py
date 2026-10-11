"""The greeter's worker side: the handler of its action, the activity its hooks call, the wiring.

Only the worker's process loads this module. ``service`` stands for the outside world a handler
reaches: it keeps what it was asked and says what a test tells it to, and a handler passes it the
action's id as the key of its side effect, as a handler of a real service must.
"""

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from temporalio import activity

from dsl.definitions import NOTE, Greet, Note, agent
from dsl.graph import final_result, initial_state
from dsl.workflow import Greeter
from dsl.workflow_passthrough import GreeterPassedThrough
from openmarketer_core.agent_runtime.langgraph_agent import AgentSteps
from openmarketer_worker import AgentRegistration
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess
from openmarketer_worker.agent_runtime.agent_worker import AgentWorker
from openmarketer_worker.agent_runtime.routes import ActionContext

TASK_QUEUE = "dsl-greeter"

worker = AgentWorker(agent, task_queue=TASK_QUEUE)


@dataclass
class GreetingService:
    """The service a greeting is sent through: every call it got, and what it does next."""

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


service = GreetingService()
notes: list[Note] = []


@worker.activity(Greet)
def greet(payload: Greet, context: ActionContext) -> Mapping[str, Any]:
    return {"text": service.greet(payload, key=context.action_id)}


@activity.defn(name=NOTE)
async def note(entry: Note) -> None:
    notes.append(entry)


def registration(checkpoints: CheckpointAccess) -> AgentRegistration:
    steps = AgentSteps(agent, initial_state=initial_state, result=final_result, recursion_limit=20)
    return worker.registration(
        checkpoints=checkpoints,
        graph=steps,
        workflows=[Greeter, GreeterPassedThrough],
        activities=[note],
    )
