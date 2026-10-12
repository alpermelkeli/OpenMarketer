"""The greeter's declarations: its actions and the limits they run under, and nothing else.

This is the module an agent's workflow may import, so it imports no graph framework and no
database: the sandbox that loads the workflow loads this. The limits are numbers no test would
choose by accident, so that a test finds them again in what Temporal was asked to schedule.
"""

from dataclasses import dataclass
from typing import TypedDict

from pydantic import BaseModel, ConfigDict

from openmarketer_core.agent_runtime.agent import Agent

KIND = "dsl-greeter"
NOTE = "note_greeting"  # the activity the workflows' hooks call


class Greeting(TypedDict):
    """The state of the greeter's graph."""

    name: str
    reply: str
    summary: str


agent = Agent(KIND, state=Greeting)


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
