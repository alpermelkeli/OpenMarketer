"""Asking the chat agent one message and storing the exchange when it answers.

``ask_project`` is the use case behind the dashboard's chat: it reads the
chat's stored conversation, hands it to the chat agent together with the
operator's new message, and - when the turn produces an answer - stores the
exchange, the question and the answer, with one write. A turn that fails stores
nothing, so sending the message again answers it again, exactly as a turn that
was lost (a process that died) is started again by sending it again.

The reads the agent makes go through ``ChatReads``, built by the caller; the
conversation is read from and written to ``db/chat.py``, and the model is the
``ChatModel`` the caller resolved. A request action (starting a research run)
reaches the caller through ``ChatRequests``, given in by whoever holds a
database and a workflow server; without one the turn can only read. This
module words no failure, starts no workflow and reaches no workflow engine.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from openmarketer_core.db.chat import ChatMessage, append_exchange, chat_history
from openmarketer_core.llm import ChatModel
from openmarketer_core.orchestrator.chat_agent.graph import answer_message
from openmarketer_core.orchestrator.chat_agent.rules import ChatLimits
from openmarketer_core.orchestrator.reads import ChatReads
from openmarketer_core.orchestrator.requests import ChatRequests


@dataclass(frozen=True)
class ChatExchange:
    """One answered exchange as it is stored, and what the turn cost to produce it.

    ``model`` and ``cost_usd`` are the turn's, not stored with the messages.
    """

    question: ChatMessage
    answer: ChatMessage
    model: str
    cost_usd: float


def ask_project(
    session: Session,
    reads: ChatReads,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    message: str,
    model: ChatModel,
    limits: ChatLimits | None = None,
    requests: ChatRequests | None = None,
) -> ChatExchange:
    """Answer ``message`` in one chat of the project, and store the exchange there.

    The turn is given the chat's stored conversation up to ``message``, and ``requests``, when
    one is given, for the request action that starts a research run. Raises
    ``ProjectNotFound`` when the workspace has no such project, ``ChatNotFound``
    when the project has no such chat, ``ChatTurnError`` when no answer was given
    within the limits and ``LLMError`` when the model cannot be used; nothing is
    stored in those cases.
    """
    history = chat_history(
        session, workspace_id=workspace_id, project_id=project_id, chat_id=chat_id
    )
    conversation: list[dict[str, Any]] = [
        {"role": stored.role.value, "content": stored.content} for stored in history
    ]
    conversation.append({"role": "user", "content": message})
    turn = answer_message(reads, model, conversation, limits=limits, requests=requests)
    question, answer = append_exchange(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        chat_id=chat_id,
        question=message,
        answer=turn.answer,
    )
    return ChatExchange(question=question, answer=answer, model=turn.model, cost_usd=turn.cost_usd)
