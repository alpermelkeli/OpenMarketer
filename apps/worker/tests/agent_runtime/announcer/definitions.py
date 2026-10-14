"""The announcer's declarations: two actions that publish, and the input its workflow takes.

``announce`` waits ten minutes for a person, longer than any test; ``announce_soon`` waits two
seconds, so that a test sees a proposal nobody decided on expire.
"""

import uuid
from dataclasses import dataclass
from typing import TypedDict

from pydantic import BaseModel, ConfigDict

from openmarketer_core.agent_runtime.actions import ActionEffect
from openmarketer_core.agent_runtime.agent import Agent
from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.agent_runtime.steps import GraphRunRef

KIND = "test-announcer"
SOON_SECONDS = 2


class Announcement(TypedDict):
    """The state of the announcer's graph: what it was told of its announcement."""

    outcome: str


@dataclass(frozen=True)
class AnnouncerContext:
    """What a run announces, and whether it asks for the action that expires soon."""

    text: str
    soon: bool


agent = Agent(KIND, state=Announcement, context=AnnouncerContext)


@agent.action("announce", effect=ActionEffect.PUBLISHES, approval_wait=600)
class Announce(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


@agent.action("announce_soon", effect=ActionEffect.PUBLISHES, approval_wait=SOON_SECONDS)
class AnnounceSoon(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


@dataclass(frozen=True)
class AnnouncerRun:
    """What the announcer's workflow is started with: the run and the project it works for."""

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID


def run_of(run: AnnouncerRun) -> GraphRunRef:
    return GraphRunRef(kind=KIND, run_id=run.run_id)


def scope_of(run: AnnouncerRun) -> RunScope:
    return RunScope(workspace_id=run.workspace_id, project_id=run.project_id)
