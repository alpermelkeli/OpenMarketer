"""The buyer's worker side: the studio it pays, its price list, and its registration."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from buyer.definitions import BuyerContext, GenerateImage, agent
from buyer.graph import initial_state
from buyer.workflow import Buyer
from openmarketer_core.agent_runtime.langgraph_agent import AgentSteps
from openmarketer_worker import AgentRegistration
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess
from openmarketer_worker.agent_runtime.agent_worker import AgentWorker
from openmarketer_worker.agent_runtime.routes import ActionContext, SpendContext

TASK_QUEUE = "test-buyer"
# Longer than the longest wait for a person (``generate_image``, ten minutes), as it must be.
LONGEST_RUN = timedelta(hours=1)
# The caps the tests' runs are given in the environment: a dollar a month, a quarter on its own.
CAPS_IN_THE_ENVIRONMENT = {"MEDIA_MONTHLY_CAP_USD": "1", "MEDIA_AUTO_APPROVE_USD": "0.25"}
# What each size costs: under the amount one action may spend alone, above it, past the cap.
PRICES = {"small": Decimal("0.10"), "large": Decimal("0.50"), "huge": Decimal("5")}


@dataclass
class Studio:
    """Where images are made: every one, with the key it was made under, and what it charged.

    ``charges`` is what the next image reports it cost (None: it reports nothing), and
    ``failures`` are raised one for each call, before anything is made.
    """

    made: list[tuple[str, str]] = field(default_factory=list)  # the size, and the key
    sizes: dict[uuid.UUID, str] = field(default_factory=dict)  # what each run asks for
    charges: str | None = None
    failures: list[Exception] = field(default_factory=list)
    estimated: list[str] = field(default_factory=list)  # the keys estimates were asked for

    def generate(self, size: str, *, key: str) -> dict[str, Any]:
        if self.failures:
            raise self.failures.pop(0)
        self.made.append((size, key))
        image: dict[str, Any] = {"image": f"image-{len(self.made)}"}
        if self.charges is not None:
            image["cost_usd"] = self.charges
        return image


def wire(
    studio: Studio,
    checkpoints: CheckpointAccess,
    sessions: sessionmaker[Session],
    environ: Mapping[str, str] = CAPS_IN_THE_ENVIRONMENT,
) -> AgentRegistration:
    worker = AgentWorker(agent, task_queue=TASK_QUEUE)

    @worker.handler(GenerateImage)
    def generate_image(payload: GenerateImage, context: ActionContext) -> Mapping[str, Any]:
        return studio.generate(payload.size, key=context.action_id)

    @worker.estimate(GenerateImage)
    def estimate_image(payload: GenerateImage, context: SpendContext) -> Decimal:
        studio.estimated.append(context.action_id)
        return PRICES[payload.size]

    steps = AgentSteps(
        agent,
        initial_state=initial_state,
        finish=lambda run, final: {"outcome": final["outcome"]},
        recursion_limit=10,
        context=lambda run, held: BuyerContext(size=studio.sizes.get(run.run_id, "small")),
    )
    return worker.registration(
        checkpoints=checkpoints,
        longest_run=LONGEST_RUN,
        graph=steps,
        workflows=[Buyer],
        sessions=sessions,
        environ=environ,
    )
