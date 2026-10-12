"""The greeter's declarations: its actions and the limits they run under, and nothing else.

This is the module an agent's workflow may import, so it imports no graph framework and no
database: the sandbox that loads the workflow loads this. The limits are numbers no test would
choose by accident, so that a test finds them again in what Temporal was asked to schedule.
"""

import uuid
from dataclasses import dataclass
from typing import TypedDict

from pydantic import BaseModel, ConfigDict

from openmarketer_core.agent_runtime.agent import Agent
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_worker.agent_runtime.vocabulary import FailureTexts

KIND = "dsl-greeter"
NOTE = "note_greeting"  # the activity the workflows' hooks call
LIMITS_OF_THE_RUN = "limits_of_the_run"  # the activity a hook asks how long a step may take


class Greeting(TypedDict):
    """The state of the greeter's graph."""

    reply: str
    summary: str


@dataclass(frozen=True)
class GreeterContext:
    """What a run of the greeter needs besides its state: whom it greets.

    The wiring builds it for each attempt, as a real agent would read its run's row.
    """

    subject: str


agent = Agent(KIND, state=Greeting, context=GreeterContext)


@agent.action("greet", timeout=17, attempts=4, retry_after=0.5, heartbeat=9)
class Greet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


# An action of another agent: asking for it is asking for one this agent never declared.
elsewhere = Agent("dsl-elsewhere", state=Greeting)


@elsewhere.action("unlisted")
class Unlisted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


@dataclass(frozen=True)
class Note:
    """What a hook tells the activity that notes it: the step, and a small detail of it."""

    step: str
    detail: str


@dataclass(frozen=True)
class Limits:
    """What the activity that knows the worker's settings says of how a run is to be taken."""

    attempt_timeout_seconds: float


@dataclass(frozen=True)
class GreetRequest:
    """What a starter of its own (the API, say) starts the greeter with: a request, not the run."""

    run_id: uuid.UUID
    requested_by: str
    patience_seconds: float  # how long one step of the run may take


def run_of_the_request(request: GreetRequest) -> GraphRunRef:
    return GraphRunRef(kind=KIND, run_id=request.run_id)


# The greeter's own words for a run that stopped without a message to show.
GREETER_TEXTS = FailureTexts(
    stopped_unexpectedly="the greeting stopped unexpectedly",
    timed_out="the greeting did not finish in time",
    worker_stopped="the worker running the greeting stopped responding",
    cancelled="the greeting was cancelled",
    database_failed="the database could not be reached",
)
# What its notes refuse with, which the greeter's workflow of its own words trusts the message of.
NOTE_REFUSED = "NoteRefused"
