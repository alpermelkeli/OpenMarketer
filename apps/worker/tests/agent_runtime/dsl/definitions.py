"""The greeter's declarations: its actions and the limits they run under, and nothing else.

This is the module an agent's workflow may import, so it imports no graph framework and no
database: the sandbox that loads the workflow loads this. The limits are numbers no test would
choose by accident, so that a test finds them again in what Temporal was asked to schedule.

Two of the greeter's actions start another workflow, ``GreetingChild`` (``child.py``): one
starts it and goes on (``RunKind.START``), one starts it and waits for it (``RunKind.WAIT``).
The child's contract is here too, because the workflow sandbox calls ``child_request_of``.
"""

import uuid
from dataclasses import dataclass
from typing import TypedDict

from pydantic import BaseModel, ConfigDict

from openmarketer_core.agent_runtime.actions import ChildRun, RunKind, RunTarget
from openmarketer_core.agent_runtime.agent import Agent
from openmarketer_core.agent_runtime.gate import RunScope
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


# The child workflow's name and task queue, as ``child.py`` registers them. A queue of its
# own, so a test can tell the child was started on the queue the target names.
CHILD_WORKFLOW = "GreetingChild"
CHILD_TASK_QUEUE = "dsl-greeter-child"
# The most a run of the child can take: the greeter's run bounds are an hour, and the tests'
# children either greet at once, fail at once, sleep a couple of seconds or wait to be
# cancelled, so two minutes are far more than any of them takes.
CHILD_LONGEST_SECONDS = 2 * 60
# What the child is told to do: greet at once, fail at once, sleep before greeting, or wait
# until it is cancelled. Its subjects -- what a run's context says to make its graph ask for
# one of these -- are in ``graph.py``, beside the other things a subject can make the graph
# ask for.
CHILD_GREETS = "greets"
CHILD_FAILS = "fails"
CHILD_GREETS_SLOWLY = "greets slowly"
CHILD_WAITS = "waits"


@dataclass(frozen=True)
class ChildRequest:
    """The child workflow's input, built from the run the runtime made for the action.

    ``run_id`` is the child run's id (its record's), and the other two are what the parent's
    payload asked for. A workflow input is stored in the engine's history, so it holds small
    values only; the child never reads a database.
    """

    run_id: uuid.UUID
    name: str
    outcome: str


def child_request_of(child: ChildRun) -> ChildRequest:
    """How a run action's payload becomes ``GreetingChild``'s input.

    A plain function of this module, because the workflow sandbox calls it. A target whose
    run stores its request with its record (as the researcher does) would build the input
    from ``child.run_id`` and ``child.workspace_id`` and ignore the payload; the greeter's
    child reads no database, so its input carries the payload's two small fields.
    """
    return ChildRequest(
        run_id=child.run_id,
        name=str(child.payload["name"]),
        outcome=str(child.payload["outcome"]),
    )


# The project the greeter's runs work for. It is fixed, because workflow code is
# deterministic and cannot make one up; the agent has actions that start another run, so
# every workflow of it says, through ``scope_of``, which project those runs belong to.
GREETERS_PROJECT = RunScope(
    workspace_id=uuid.UUID("00000000-0000-4000-8000-0000000000a1"),
    project_id=uuid.UUID("00000000-0000-4000-8000-0000000000a2"),
)


def the_greeters_project(_: object) -> RunScope:
    return GREETERS_PROJECT


agent = Agent(KIND, state=Greeting, context=GreeterContext)


@agent.action("greet", timeout=17, attempts=4, retry_after=0.5, heartbeat=9)
class Greet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


@agent.action(
    "wait_for_a_greeting",
    run=RunTarget(
        workflow=CHILD_WORKFLOW,
        task_queue=CHILD_TASK_QUEUE,
        kind=RunKind.WAIT,
        longest_seconds=CHILD_LONGEST_SECONDS,
        input_of=child_request_of,
    ),
)
class WaitForAGreeting(BaseModel):
    """A greeting asked of another run, which the greeter waits for."""

    model_config = ConfigDict(extra="forbid")

    name: str
    outcome: str = CHILD_GREETS


@agent.action(
    "start_a_greeting",
    run=RunTarget(
        workflow=CHILD_WORKFLOW,
        task_queue=CHILD_TASK_QUEUE,
        kind=RunKind.START,
        longest_seconds=CHILD_LONGEST_SECONDS,
        input_of=child_request_of,
    ),
)
class StartAGreeting(BaseModel):
    """A greeting another run is asked for; the greeter goes on without waiting."""

    model_config = ConfigDict(extra="forbid")

    name: str
    outcome: str = CHILD_GREETS


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
