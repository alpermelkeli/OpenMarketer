"""The announcer's graph: one node that asks to publish, and says what came of it."""

from typing import Any

from announcer.definitions import Announce, Announcement, AnnouncerContext, AnnounceSoon, agent
from openmarketer_core.agent_runtime.agent import END
from openmarketer_core.agent_runtime.langgraph_steps import request_action
from openmarketer_core.agent_runtime.steps import GraphRunRef


def initial_state(run: GraphRunRef) -> dict[str, Any]:
    return {"outcome": ""}


@agent.node(start=True)
def announce(state: Announcement, context: AnnouncerContext) -> dict[str, Any]:
    payload = AnnounceSoon(text=context.text) if context.soon else Announce(text=context.text)
    outcome = request_action(payload)
    if outcome.succeeded:
        return {"outcome": f"published as {outcome.result['post']}"}
    return {"outcome": f"not published: {outcome.error}"}


agent.edge(announce, END)
