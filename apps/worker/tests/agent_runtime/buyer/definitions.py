"""The buyer's declarations: one action that spends, and the input its workflow takes.

The image's size is all the payload holds: what it costs is the price list's, which the
worker's estimate reads (``wiring.py``), never a number the model wrote.
"""

import uuid
from dataclasses import dataclass
from typing import TypedDict

from pydantic import BaseModel, ConfigDict

from openmarketer_core.agent_runtime.actions import ActionEffect
from openmarketer_core.agent_runtime.agent import Agent
from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.agent_runtime.steps import GraphRunRef

KIND = "test-buyer"
# What the studio's handler refuses with when it cannot make the image: worded, and shown.
STUDIO_REFUSED = "StudioRefused"


class Spending(TypedDict):
    """The state of the buyer's graph: what it was told of its image."""

    outcome: str


@dataclass(frozen=True)
class BuyerContext:
    """The size of image a run asks for."""

    size: str


agent = Agent(KIND, state=Spending, context=BuyerContext)


@agent.action(
    "generate_image", effect=ActionEffect.SPENDS, approval_wait=600, attempts=2, retry_after=0.2
)
class GenerateImage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    size: str


@dataclass(frozen=True)
class BuyerRun:
    """What the buyer's workflow is started with: the run and the project it works for."""

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID


def run_of(run: BuyerRun) -> GraphRunRef:
    return GraphRunRef(kind=KIND, run_id=run.run_id)


def scope_of(run: BuyerRun) -> RunScope:
    return RunScope(workspace_id=run.workspace_id, project_id=run.project_id)
