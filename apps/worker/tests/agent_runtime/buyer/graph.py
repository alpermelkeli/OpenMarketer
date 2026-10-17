"""The buyer's graph: one node that asks for an image, and says what came of it."""

from typing import Any

from buyer.definitions import BuyerContext, GenerateImage, Spending, agent
from openmarketer_core.agent_runtime.agent import END
from openmarketer_core.agent_runtime.langgraph_steps import request_action
from openmarketer_core.agent_runtime.steps import GraphRunRef


def initial_state(run: GraphRunRef) -> dict[str, Any]:
    return {"outcome": ""}


@agent.node(start=True)
def generate(state: Spending, context: BuyerContext) -> dict[str, Any]:
    outcome = request_action(GenerateImage(size=context.size))
    if outcome.succeeded:
        return {"outcome": f"made {outcome.result['image']}"}
    return {"outcome": f"not made: {outcome.error}"}


agent.edge(generate, END)
