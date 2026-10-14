"""The announcer's worker side: the board it publishes to, and its registration."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from announcer.definitions import Announce, AnnouncerContext, AnnounceSoon, agent
from announcer.graph import initial_state
from announcer.workflow import Announcer
from openmarketer_core.agent_runtime.langgraph_agent import AgentSteps
from openmarketer_worker import AgentRegistration
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess
from openmarketer_worker.agent_runtime.agent_worker import AgentWorker
from openmarketer_worker.agent_runtime.routes import ActionContext

TASK_QUEUE = "test-announcer"


@dataclass
class Board:
    """Where announcements are published: every one, with the key it was published under."""

    published: list[tuple[str, str]] = field(default_factory=list)  # the text, and the key
    texts: dict[uuid.UUID, str] = field(default_factory=dict)  # what each run announces
    soon: set[uuid.UUID] = field(default_factory=set)  # the runs that ask for announce_soon

    def publish(self, text: str, *, key: str) -> str:
        self.published.append((text, key))
        return f"post-{len(self.published)}"


def wire(
    board: Board, checkpoints: CheckpointAccess, sessions: sessionmaker[Session]
) -> AgentRegistration:
    worker = AgentWorker(agent, task_queue=TASK_QUEUE)

    @worker.handler(Announce)
    def announce(payload: Announce, context: ActionContext) -> Mapping[str, Any]:
        return {"post": board.publish(payload.text, key=context.action_id)}

    @worker.handler(AnnounceSoon)
    def announce_soon(payload: AnnounceSoon, context: ActionContext) -> Mapping[str, Any]:
        return {"post": board.publish(payload.text, key=context.action_id)}

    steps = AgentSteps(
        agent,
        initial_state=initial_state,
        finish=lambda run, final: {"outcome": final["outcome"]},
        recursion_limit=10,
        context=lambda run, held: AnnouncerContext(
            text=board.texts.get(run.run_id, "v2 is out"), soon=run.run_id in board.soon
        ),
    )
    return worker.registration(
        checkpoints=checkpoints, graph=steps, workflows=[Announcer], sessions=sessions
    )
