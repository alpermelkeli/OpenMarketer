"""Setting the orchestrator's follow-up turn up for this installation, when the worker starts.

The model configuration is read and checked now, so a mistake in it stops the worker at
start-up. No credential and no choice of a workspace is read here: the follow-up asks for the
model of the chat's workspace, with the key it has at that moment, at each attempt
(``new_model``), as the researcher's wiring does for its models. The result is what the
process registers: the ``ChatFollowUp`` workflow and its two activities, on the orchestrator's
own task queue. A follow-up keeps no graph checkpoints (the chat agent runs in memory, within
one attempt), so there is no cleanup of them.

This module holds no rule and does not connect to Temporal. The workflow never imports it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.llm import ChatModel, RouterChatModel
from openmarketer_core.llm_config import DEFAULT_CONFIG_PATH, ModelRouter
from openmarketer_core.orchestrator.chat_agent.rules import ChatLimits
from openmarketer_core.tracing import Tracing
from openmarketer_worker import AgentRegistration
from openmarketer_worker.credentials import WorkspaceCredentials
from openmarketer_worker.orchestrator.activities import FollowUpActivities
from openmarketer_worker.orchestrator.contract import CHAT_FOLLOW_UP_TASK_QUEUE
from openmarketer_worker.orchestrator.workflow import ChatFollowUp
from openmarketer_worker.workspace_settings import WorkspaceSettings


@dataclass(frozen=True)
class OrchestratorParts:
    """What the follow-up's worker side is made of, built once when the worker starts."""

    sessions: sessionmaker[Session]
    # The chat model of one attempt, made for the chat's workspace when it is needed, so no
    # credential or choice of the workspace is held between attempts.
    new_model: Callable[[uuid.UUID], ChatModel]
    tracing: Tracing
    limits: ChatLimits | None = None


def orchestrator(
    environ: Mapping[str, str],
    sessions: sessionmaker[Session],
    credentials: WorkspaceCredentials,
    tracing: Tracing,
) -> AgentRegistration:
    """The follow-up turn as this installation runs it, ready for the worker."""
    router = ModelRouter.from_file(
        environ.get("LLM_CONFIG_PATH") or DEFAULT_CONFIG_PATH, env=environ
    )
    workspace_settings = WorkspaceSettings(sessions)

    def new_model(workspace_id: uuid.UUID) -> ChatModel:
        # The workspace's model choices laid over the file, and the key it has now; traced
        # when tracing is on, each call a generation under the turn.
        chosen = workspace_settings.router_for(workspace_id, router)
        key = credentials.model_key(workspace_id, chosen)
        return tracing.model(RouterChatModel(router=chosen, api_key=key))

    return registration(OrchestratorParts(sessions=sessions, new_model=new_model, tracing=tracing))


def registration(parts: OrchestratorParts) -> AgentRegistration:
    """What the worker registers to follow up in chats with ``parts``."""
    activities = FollowUpActivities(
        parts.sessions, new_model=parts.new_model, tracing=parts.tracing, limits=parts.limits
    )
    return AgentRegistration(
        task_queue=CHAT_FOLLOW_UP_TASK_QUEUE,
        workflows=[ChatFollowUp],
        activities=[activities.follow_up_on_research, activities.record_follow_up_not_taken],
        checkpoint_cleanups=[],
    )
